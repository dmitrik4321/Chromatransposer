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
from ct.core.ir import EffectIR, NativeRef, PortResult, Segment
from ct.core.mqtt_bridge import MqttBridge
from ct.core.mqtt_broker import EmbeddedBroker
from ct.core.orchestrator import Caller, Group, Orchestrator, Scene, Schedule
from ct.core.registry import Registry
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
        # транслятор: карты/шкалы/лестница деградации из YAML (без хардкода)
        configure_translator(self.config.get("translator", {}))

        mqtt_cfg = self.config.get("mqtt", {})
        self.bridge = MqttBridge(mqtt_cfg.get("host", "127.0.0.1"),
                                 mqtt_cfg.get("port", 1883))
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
            for node in await adapter.discover():
                self.registry.upsert(node)
                await adapter.subscribe(node.id,
                                        self._make_state_cb(node.id))

        # нормализация входящих телеметрий-топиков экосистем в канон
        self.bridge.on("ct/v1/#/telemetry/#", self._on_telemetry)

        tick_hz = self.config.get("sync", {}).get("tick_hz", 5)
        await self.orchestrator.start_tick(1.0 / tick_hz)

        for g in self.config.get("groups", []):
            self.orchestrator.groups[g["id"]] = Group(**g)
        for s in self.config.get("scenes", []):
            self.orchestrator.scenes[s["id"]] = Scene(**s)
        for sc in self.config.get("schedules", []):
            self.orchestrator.schedules[sc["id"]] = Schedule(**sc)
        self._recon_task = asyncio.create_task(self._reconcile_loop())

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
        for a in self.adapters.values():
            if a.name == node.adapter:
                return a
        raise KeyError(f"no adapter '{node.adapter}' loaded")

    async def stop(self) -> None:
        recon = getattr(self, "_recon_task", None)
        if recon:
            recon.cancel()
        await self.orchestrator.stop_tick()
        if self.bridge:
            await self.bridge.close()
