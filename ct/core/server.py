"""Сервер Chromatransposer: сборка реестра, адаптеров, оркестратора, MQTT.

Офлайн-устойчивость: очередь команд с ретраями для недоступных устройств.
Конфиг как код: configs/server.yaml.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import time
from typing import Any, Optional

import yaml

from ct.adapters import ADAPTERS
from ct.adapters.base import SouthboundAdapter, UnsupportedError
from ct.core.auth import Provisioning
from ct.core.ha_bridge import HaBridge
from ct.core.ha_discovery import HaDiscoveryExporter
from ct.core.ir import EffectIR, NativeRef, PortResult, Segment
from ct.core.mqtt_bridge import MqttBridge
from ct.core.mqtt_broker import EmbeddedBroker
from ct.core.orchestrator import Caller, Group, Orchestrator, Scene, Schedule
from ct.core.registry import Registry
from ct.core.render import RENDER_CONFIG, configure as configure_render
from ct.core.translator import configure as configure_translator, ir_to_native, native_to_ir

# ------------------------------ КОНФИГ СЕРВЕРА (дефолты) ----------------------
# Всё меняемое — здесь; переопределяется configs/server.yaml (конфиг как код).
SERVER_CONFIG = {
    "mqtt": {
        "host": "127.0.0.1", "port": 1883,          # адрес БРОКЕРА для клиента моста
        "broker": {"enabled": True},                # встроенный брокер: прослойка
                                                    # сама поднимает MQTT (решение D)
    },
    "homeassistant": {                               # HA тоже нужен (требование)
        "mode": "embedded",       # embedded|external|off — прослойка сама поднимает HA
        "container": "chromatransposer-ha",
        "image": "homeassistant/home-assistant:latest",
        "port": 8123,
        "token_file": "state/ha_token",             # long-lived токен для discovery-экспорта
        "startup_timeout_s": 90,
    },
    "state_dir": "state",
    "sync": {"tick_hz": 5},                        # общий тик стендов (решение B)
    "reconcile_interval_s": 30.0,                  # периодическая реконсиляция
    "generic_dir": "configs/generic",              # YAML-маппинги «лапши»
    "render": {                                    # ASCII-предпросмотр в CLI
        "fps": 10.0, "cell_width": 2, "zone_height": 24,
        # типизация: явный device_type > labels.type > эвристика по ключевым словам
        "type_heuristics": {"tree": ["ёлк", "елк", "tree", "garland"]},
    },
}


class CommandQueue:
    """Очередь команд для офлайна: device_id -> deque(payload). Ретраи при появлении."""

    def __init__(self, max_per_device: int = SERVER_CONFIG.get("queue_depth", 50)):
        self.q: dict[str, collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=max_per_device))

    def push(self, device_id: str, payload: dict) -> None:
        self.q[device_id].append({"ts": time.time(), **payload})

    def drain(self, device_id: str) -> list[dict]:
        items = list(self.q.pop(device_id, []))
        return items


class Server:
    def __init__(self, config_path: str = "configs/server.yaml"):
        self.config = {**SERVER_CONFIG, **self._load_config(config_path)}
        self.registry = Registry()
        self.bridge: Optional[MqttBridge] = None
        # инфраструктурные сервисы (решение D + требование «сама поднимает»):
        self.broker = EmbeddedBroker(self.config.get("mqtt", {}).get("broker", {}))
        ha_cfg = dict(self.config.get("homeassistant", {}))
        if not self.broker.cfg.get("enabled", True):
            # внешний брокер: HA должен смотреть в него, а не на 127.0.0.1 внутри контейнера
            mcfg = self.config.get("mqtt", {})
            ha_cfg.setdefault("mqtt_bind", f"{mcfg.get('host', '127.0.0.1')}:{mcfg.get('port', 1883)}")
        else:
            ha_cfg.setdefault("mqtt_bind", self.broker.cfg["bind"])
        self.ha = HaBridge(ha_cfg)
        self.discovery: Optional[HaDiscoveryExporter] = None
        self._ha_task: Optional[asyncio.Task] = None
        self._stopping = False
        self.provisioning = Provisioning(self.config.get("state_dir", "state"))
        self.queue = CommandQueue()
        self.orchestrator = Orchestrator(self.registry, self._apply)
        self.adapters: dict[str, SouthboundAdapter] = {}
        self.telemetry_latest: dict[str, dict] = {}   # device_id -> последний Telemetry-снапшот

    @staticmethod
    def _load_config(path: str) -> dict:
        if os.path.exists(path):
            with open(path) as f:
                return yaml.safe_load(f) or {}
        return {}

    # --- запуск ------------------------------------------------------------------
    async def start(self) -> None:
        # 1) встроенный MQTT-брокер (решение D): прослойка сама поднимает MQTT.
        await self.broker.start()

        # транслятор: карты/шкалы/лестница деградации из YAML (без хардкода)
        configure_translator(self.config.get("translator", {}))
        # ASCII-предпросмотр: рендер-конфиг тоже из YAML
        configure_render({**RENDER_CONFIG, **self.config.get("render", {})})
        # плоское состояние -> IR для предпросмотра (ключи разбора из YAML)
        from ct.core.state_ir import configure as configure_state_ir
        configure_state_ir(self.config.get("state_ir", {}))

        mqtt_cfg = self.config.get("mqtt", {})
        # если брокер встроенный — клиент моста идёт на фактический bind-адрес
        # (тесты/несколько инстансов могут использовать нестандартный порт)
        host, port = mqtt_cfg.get("host", "127.0.0.1"), mqtt_cfg.get("port", 1883)
        if self.broker.running:
            bhost, _, bport = self.broker.bound_bind().rpartition(":")
            host, port = (bhost or "127.0.0.1"), int(bport)
        self.bridge = MqttBridge(host, port)
        await self.bridge.connect()
        await self.bridge.publish_lwt("ct/v1/server/available")
        await self.bridge.publish("ct/v1/server/available",
                                  {"state": "online"}, retain=True)

        for spec in self.config.get("adapters", []):
            cls = ADAPTERS[spec["type"]]
            kwargs = {k: v for k, v in spec.items() if k != "type"}
            kwargs.setdefault("bridge", self.bridge)
            if spec["type"] == "generic" and "configs" not in kwargs:
                kwargs["configs"] = self._load_generic_configs(
                    self.config.get("generic_dir", "configs/generic"))
            adapter = cls(**kwargs)
            self.adapters[adapter.name + "-" + str(len(self.adapters))] = adapter
            discovered = await adapter.discover()
            if discovered:
                # узлы known-at-discovery: обычный путь — upsert в реестр
                for node in discovered:
                    self.registry.upsert(node)
                    await adapter.subscribe(node.id,
                                            self._make_state_cb(node.id))
            elif hasattr(adapter, "on_node_registered"):
                # ленивые адаптеры (opera_mqtt): узлы рождаются из трафика;
                # колбэк даёт им доступ к реестру/оркестратору/discovery —
                # подписки на состояние адаптер ведёт сам (см. _register_node)
                adapter.on_node_registered = self._register_lazy_node

        # нормализация входящих телеметрий-топиков экосистем в канон
        self.bridge.on("ct/v1/+/telemetry/#", self._on_telemetry)  # + ровно один уровень: # в середине запрещён MQTT

        tick_hz = self.config.get("sync", {}).get("tick_hz", 5)
        if tick_hz > 0:                      # 0 — тик выключен конфигом
            await self.orchestrator.start_tick(1.0 / tick_hz)

        for g in self.config.get("groups", []):
            self.orchestrator.groups[g["id"]] = Group(**g)
        for s in self.config.get("scenes", []):
            self.orchestrator.scenes[s["id"]] = Scene(**s)
        for sc in self.config.get("schedules", []):
            self.orchestrator.schedules[sc["id"]] = Schedule(**sc)
        # расписания (every/at/daily) живут в цикле оркестратора; тот стартует
        # только при включённом тике, поэтому отдельный планировщик здесь
        if self.orchestrator._sched_task is None:
            self._sched_task = asyncio.create_task(self.orchestrator._scheduler_loop())
        self._recon_task = asyncio.create_task(self._reconcile_loop())

        # 2) HA: discovery-экспорт + фоновый подъём контейнера (не блокирует старт).
        self.discovery = HaDiscoveryExporter(
            self.bridge, self.registry,
            self.config.get("homeassistant", {}).get("discovery"),
            adapters=self.adapters)      # политики «что скрыть из UI HA» — у адаптеров
        await self.discovery.publish_all()
        if self.config.get("homeassistant", {}).get("mode", "embedded") != "off":
            self._ha_task = asyncio.create_task(self._ha_bootstrap())

    async def _ha_bootstrap(self) -> None:
        """HA поднимается фоном с retry: ядро не должно ждать/падать из-за него.

        Ждём readiness урывками и реагируем на отмену — иначе `stop()` висел бы
        все startup_timeout_s в executor-потоке (проверено тестами).
        """
        deadline_s = self.config.get("homeassistant", {}).get("startup_timeout_s", 90)
        try:
            if await self.ha.ensure_running():
                ok = await self._wait_ready_cancellable(deadline_s)
            else:
                ok = False
            if not ok and not self._stopping:
                # одна повторная попытка после паузы (допустим, докер ещё не поднялся)
                await asyncio.sleep(min(deadline_s, 15))
                await self.ha.ensure_running()
        except asyncio.CancelledError:
            pass
        except Exception as e:                       # не роняем сервер из-за HA
            self.ha.last_error = f"bootstrap: {e}"

    async def _wait_ready_cancellable(self, deadline_s: float) -> bool:
        waited = 0.0
        while waited < deadline_s and not self._stopping:
            ready = asyncio.ensure_future(
                asyncio.get_running_loop().run_in_executor(None, self.ha._probe_sync))
            done, _ = await asyncio.wait({ready}, timeout=2)
            if done and ready.result():
                return True
            if not done:
                ready.cancel()
            await asyncio.sleep(2)
            waited += 2
        self.ha.last_error = f"HA не поднялся за {deadline_s}s"
        return False

    @staticmethod
    def _load_generic_configs(directory: str) -> list[dict]:
        out = []
        if os.path.isdir(directory):
            for fn in sorted(os.listdir(directory)):
                if fn.endswith((".yaml", ".yml")):
                    with open(os.path.join(directory, fn)) as f:
                        out.append(yaml.safe_load(f))
        return out

    async def _reconcile_loop(self) -> None:
        """Периодическая реконсиляция desired vs reported (дрейф правится выдачей)."""
        interval = self.config.get("reconcile_interval_s", 30.0)
        while True:
            try:
                await asyncio.sleep(interval)
                await self.orchestrator.reconcile()
            except asyncio.CancelledError:
                break
            except Exception:
                continue  # офлайн-устойчивость: цикл не должен умирать

    def _make_state_cb(self, device_id: str):
        async def cb(state: dict) -> None:
            self.orchestrator.report_state(device_id, state)
            await self.bridge.publish(self.bridge.t_state(device_id), state, retain=True)
        return cb

    async def _register_lazy_node(self, node) -> None:
        """Регистрация узла, рождённого из входящего трафика (opera_mqtt).

        Делает ровно то же, что путь discover(): upsert в реестр + подписка на
        состояние (репорт оркестратору и публикация canonical state), плюс
        discovery-экспорт в HA для нового устройства. Колбэк вызывается
        адаптером синхронно из обработчика MQTT-сообщения."""
        self.registry.upsert(node)
        adapter = self._adapter_for(node)
        await adapter.subscribe(node.id, self._make_state_cb(node.id))
        if getattr(self, "discovery", None) is not None:
            await self.discovery.publish_device(node)
            if node.telemetry_kinds:
                await self.discovery.publish_telemetry_sensors(
                    node.id, node.telemetry_kinds)

    async def _on_telemetry(self, topic: str, payload: dict) -> None:
        parts = topic.split("/")
        dev, kind = parts[2], parts[-1]
        snap = {"device_id": dev, "kind": kind, "value": payload.get("value"),
                "meta": payload.get("meta", {}), "ts": time.time()}
        self.telemetry_latest[f"{dev}/{kind}"] = snap
        # прокидываем телеметрию синхронизируемым устройствам, которым она нужна
        for g in self.orchestrator.groups.values():
            if not g.sync:
                continue
            members = self.orchestrator.group_members(g)
            if dev in members:
                for other in members:
                    if other != dev:
                        await self.bridge.publish(
                            self.bridge.t_set(other),
                            {"sensor": snap}, qos=0)

    # --- единая точка выдачи команды на юг -----------------------------------------
    async def _apply(self, device_id: str, state: dict, segment: Optional[Segment],
                     ir: Optional[EffectIR]) -> dict[str, Any]:
        node = self.registry.get(device_id)
        adapter = self._adapter_for(node)
        result: dict[str, Any] = {"device": device_id}

        if ir is not None:
            pr = self.port_ir(ir, node)
            if pr.unsupported:
                raise UnsupportedError("; ".join(pr.notes))
            if node.supports_ir and pr.target_native:
                result["ir"] = await adapter.apply_ir(device_id, ir, segment)
            elif pr.target_native:
                st = {"native": pr.target_native.args}
                result["native"] = await adapter.set_state(device_id, st, segment)
            if pr.degraded_to:
                result["degraded"] = pr.degraded_to.losses
        elif state:
            try:
                result["state"] = await adapter.set_state(device_id, state, segment)
            except Exception:
                self.queue.push(device_id, {"state": state})  # офлайн-очередь
                result["queued"] = True
        # сброс накопленного при возвращении онлайн
        if node.online.value == "online" and self.queue.q.get(device_id):
            for item in self.queue.drain(device_id):
                await adapter.set_state(device_id, item.get("state", {}), segment)
            result["flushed_queue"] = True
        return result

    def port_ir(self, ir: EffectIR, target) -> PortResult:
        return ir_to_native(ir, target)

    def port_native(self, source_ref: NativeRef, target_id: str,
                    args: Optional[dict] = None) -> PortResult:
        """Портирование: native(src) -> IR -> native(target)."""
        ir = native_to_ir(source_ref, args)
        return ir_to_native(ir, self.registry.get(target_id))

    def _adapter_for(self, node) -> SouthboundAdapter:
        # узел хранит имя класса адаптера (node.adapter), а ключ реестра
        # адаптеров — "<name>-<n>" (несколько экземпляров одного класса);
        # ищем по имени класса, как в exporter'е.
        for a in self.adapters.values():
            if a.name == node.adapter:
                return a
        raise KeyError(f"no adapter '{node.adapter}' loaded")

    async def stop(self) -> None:
        self._stopping = True
        recon = getattr(self, "_recon_task", None)
        if recon:
            recon.cancel()
        sched = getattr(self, "_sched_task", None)
        if sched:
            sched.cancel()
        ha_task = getattr(self, "_ha_task", None)
        if ha_task:
            ha_task.cancel()
        await self.orchestrator.stop_tick()
        if self.bridge:
            await self.bridge.close()
        # брокер — часть этого процесса: останавливаем последним, чтобы
        # bridge/LWT-«offline» успели уйти в шину
        await self.broker.stop()

    def infra_status(self) -> dict:
        """Состояние инфраструктурных сервисов для /status (CLI и REST)."""
        return {
            "mqtt_broker": {"embedded": self.broker.cfg.get("enabled", True),
                            "running": self.broker.running,
                            "bind": self.broker.cfg["bind"]},
            "homeassistant": self.ha.status(),
        }
