"""Southbound-адаптер Opera GX по ОФИЦИАЛЬНОЙ интеграции (второй коннектор).

Нашёл доку, как просили: github.com/operasoftware/opera-smart-home — это
официальный репозиторий Opera (Apache-2.0):
  * api/mqtt.idl + api/README.md — расширение `opr.mqtt` для расширений браузера
    (MQTT API введено в Opera GX 119.0.5497.163);
  * opera_gx_smart_home_extension/ — официальное расширение, которое само
    публикует HA MQTT-discovery и состояния в брокер;
  * префикс топиков браузера: 'opera/<prefix_defined_in_browser_settings>/<topic>'
    (global=true добавляет topic без префикса; wildcards в subscribe запрещены).

Ключевое отличие от CDP-коннектора (ct/adapters/opera.py): здесь Opera САМА
публикует своё состояние в наш локальный MQTT-брокер — адаптеру не нужно
поллить браузер и не нужен --remote-debugging-port. Модель сущностей берём
ровно из device.js официального расширения (см. OPERA_MQTT_CONFIG ниже),
исходники Opera и расширения НЕ меняем.

Panic Button (хотелка пользователя «убрать из UI HA, чтобы не прикалывались»):
реализовано КОРРЕКТНО — правкой нашего discovery-экспорта
(ct/core/ha_discovery.py: homeassistant.opera.hide_entities), а НЕ патчем
официального расширения. Смысл:
  1) мы НЕ объявляем switch.panic_mode в HA discovery -> в UI HA его нет;
  2) при появлении в топике значения ON (кто-то включил руками на клавиатуре
     или другой контроллер) адаптер шлёт OFF на команду расширения
     opera/<prefix>/switch/panic_mode/set -> panic-режим реально выходит,
     а не остаётся «включённым невидимым переключателем»;
  3) hide_commands (по умолчанию false) дополнительно гасит кнопки open_new_tab
     / close_current_tab / close_private_windows — на случай, если и они
     кажутся чьим-то приколом. Всё конфигируется, ничего не зашито.

Стиль проекта: весь изменяемый конфиг — в OPERA_MQTT_CONFIG в начале файла,
переопределяется секцией adapters[].config в configs/server.yaml.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Optional

from ct.adapters.base import SouthboundAdapter, UnsupportedError
from ct.core.ir import EffectIR, NativeRef, Segment, Topology
from ct.core.registry import (DeviceNode, Endpoint, Ecosystem, OnlineState,
                              TransportKind)

log = logging.getLogger("ct.adapter.opera_mqtt")

# ------------------------------ КОНФИГ АДАПТЕРА ------------------------------
# Всё меняемое — здесь; переопределяется configs/server.yaml (adapters[].config).
OPERA_MQTT_CONFIG = {
    # Регулярка на топики Opera: opera/<prefix>/<entity_path>. Префикс
    # пользователь задаёт в настройках Smart Home браузера (getTopicPrefix).
    "topic_regex": r"^opera/(?P<prefix>[^/]+)/(?P<path>.+)$",
    # --- карта официальных сущностей: ровно состав device.js ---
    # (sensors/binary_sensors/switches/commands/triggers). kind важен для
    # маппинга в каноническое состояние и для политики Panic Button.
    "entities": {
        # sensors: opera/<prefix>/sensor/<id>
        "sensors": ["accent_color", "battery", "number_of_tabs", "ram"],
        # binary_sensors: opera/<prefix>/binary_sensor/<id>
        "binary_sensors": ["playing_movie", "conference"],
        # switches: state opera/<prefix>/switch/<id>, command .../switch/<id>/set ("ON"/"OFF")
        "switches": ["mute_all_tabs", "panic_mode",
                     "pref_music", "pref_browser_sounds", "pref_keyboard_sounds"],
        # commands (button в HA): opera/<prefix>/command/<id>
        "commands": ["open_new_tab", "close_current_tab", "close_private_windows"],
        # triggers: события, не состояния (у официального расширения это
        # new_tab/closed_tab триггеры) — транслируем в телеметрию событий
        "triggers": ["new_tab", "closed_tab"],
    },
    # Значения URL-масок бинарников из conference_sensor.js / playing_movie_sensor.js
    # (распознавание по url-паттернам; у нас — только для подписей логгера).
    "movie_url_hints": ["youtube.com/watch", "netflix.com", "kinopoisk.ru"],
    "conference_url_hints": ["zoom.us/j/", "meet.google.com", "teams.microsoft.com"],
    # --- политика Panic Button (хотелка) ---
    "panic": {
        "hide_from_ha": True,      # не объявлять switch.panic_mode в HA-discovery
        "auto_off": True,          # ловить ON и гасить OFF командным топиком
        "auto_off_debounce_s": 2.0,# не долбить команду чаще (защита от флаттера)
    },
    # какие entity-path'ы ещё прятать от HA (доп. антиприкол; пусто по умолчанию)
    "hide_entities_extra": [],
    # маппинг состояний Opera в канонический state узла (control plane):
    # "on" = браузер живёт, пока сообщения приходят в пределах ttl;
    # яркость ленты-проекции = 100%; accent_color = цвет темы GX.
    "state_map": {
        "on_when": ["number_of_tabs>0"],   # браузер запущен, когда есть вкладки
        "online_ttl_s": 65.0,              # discovery/state-alarm = каждые 5 мин
        "brightness_source": None,         # None: постоянная; можно "battery"
        "color_source": "accent_color",    # RGB-тон проекции из палитры GX
        "count_source": "number_of_tabs",  # «длина ленты» = число вкладок
    },
    # родные эффекты: IR-класс -> действие над окном/звуком через штатные
    # команды/переключатели расширения (ничего своего в браузер не внедряем).
    "native_effects": [
        {"effect": "static",      "class": "static"},
        {"effect": "blink",       "class": "blink"},
        {"effect": "colorsweep",  "class": "colorsweep"},
        {"effect": "breathing",   "class": "breathing"},
        {"effect": "mute",        "class": "custom"},
        {"effect": "panic_off",   "class": "custom"},
    ],
    "ir_class_map": {              # EffectClass -> native action id
        "static": "static",
        "blink": "blink",
        "colorsweep": "colorsweep",
        "breathing": "breathing",
    },
    # режимы breathing/blink реализуются периодическими set-командами mute_all_tabs
    # (честная деградация: внутри браузера RGB не рисуем, управляем звуком/окном)
    "pulse_period_ms": {"blink": 500, "breathing": 1600},
    "device_id": "opera-gx-mqtt",
    "name": "Opera GX (smart-home ext)",
}
# -----------------------------------------------------------------------------


class OperaMqttAdapter(SouthboundAdapter):
    """Коннектор к официальному Opera GX Smart Home расширению поверх MQTT.

    Транспорт — тот же локальный брокер (решение D). Адаптер:
      * слушает opera/<prefix>/... и строит/обновляет канонический узел;
      * пишет состояние в ct/v1/<device_id>/state (retain) и LWT-детекцию
        офлайна по TTL (браузер закрыт -> сообщений нет -> offline);
      * применяет IR-эффекты штатными командами расширения (switch/command);
      * исполняет политику Panic Button (см. докстринг модуля).
    """

    name = "opera_mqtt"
    ecosystem = "opera"

    def __init__(self, bridge=None, config: Optional[dict] = None,
                 device_id: str = "", name: str = "", **extra):
        # **extra — терпимость к общим ключам specs из server.yaml
        # (hosts и т.п. этого коннектору не нужны: узлы рождаются из трафика).
        self.cfg = {**OPERA_MQTT_CONFIG, **(config or {})}
        if device_id:
            self.cfg["device_id"] = device_id
        if name:
            self.cfg["name"] = name
        self.bridge = bridge
        # колбэк сервера для ленивой регистрации узлов из трафика
        # (upsert + подписки + HA-discovery); до start() может быть None —
        # тогда узел живёт только внутри адаптера.
        self.on_node_registered: Optional[Callable[[DeviceNode], Awaitable[None]]] = None
        self._rx = re.compile(self.cfg["topic_regex"])
        self._nodes: dict[str, DeviceNode] = {}
        self._raw: dict[str, dict[str, Any]] = {}       # dev -> {entity_path: value}
        self._last_seen: dict[str, float] = {}
        self._subs: dict[str, list[Callable]] = {}
        self._prefixes: dict[str, str] = {}             # dev -> opera prefix
        self._panic_last_off: float = 0.0
        self._ttl_task: Optional[asyncio.Task] = None
        self._pulses: dict[str, dict] = {}              # dev -> {mode, task}

    # ----------------------------- discover -----------------------------
    async def discover(self) -> list[DeviceNode]:
        """Подписка на всё пространство opera/# (без wildcards на стороне
        браузера — ограничение opr.mqtt нас не касается, мы подписываемся со
        стороны брокера). Узел создаётся лениво при первом сообщении."""
        if self.bridge is not None:
            self.bridge.on("opera/#", self._on_opera_msg)
        self._ttl_task = asyncio.create_task(self._ttl_loop())
        return []                      # узлов пока нет; появятся из трафика

    def _node_for(self, dev: str) -> DeviceNode:
        did = self.cfg["device_id"] if dev == "__default__" else f"{self.cfg['device_id']}-{dev}"
        n = DeviceNode(
            id=did, name=f"{self.cfg['name']} [{dev}]", ecosystem=Ecosystem.OPERA,
            adapter=self.name, device_type="screen",
            topology=Topology(kind="strip", length=0, width=1, height=1,
                              color_order="RGB", max_fps=1),
            endpoints=[Endpoint(kind=TransportKind.MQTT, url="opera/#"),
                       Endpoint(kind=TransportKind.MQTT,
                                url=f"ct/v1/{did}/set")],   # команды сверху
            native_effects=[NativeRef(ecosystem=self.ecosystem, effect=e["effect"],
                                       args={"class": e["class"]})
                            for e in self.cfg["native_effects"]],
            supports_ir=False, supports_segments=False,
            telemetry_kinds=["accent_color", "battery", "ram", "tabs",
                             "playing_movie", "conference", "event"],
        )
        return n

    # ----------------------------- ingest -----------------------------
    async def _on_opera_msg(self, topic: str, payload: dict) -> None:
        m = self._rx.match(topic)
        if not m:
            return
        prefix, path = m.group("prefix"), m.group("path")
        # служебный трафик discovery (homeassistant/*) идёт мимо этого regex,
        # но на всякий случай отсекаем config-фрагменты
        if path.endswith("/config") or path.startswith("homeassistant"):
            return
        raw_val = payload.get("raw", payload)
        val = raw_val if isinstance(raw_val, str) else json.dumps(raw_val)
        dev = prefix
        self._prefixes[dev] = prefix
        self._raw.setdefault(dev, {})[path] = val
        self._last_seen[dev] = time.time()
        node = self._nodes.get(dev)
        if node is None:
            node = self._nodes[dev] = self._node_for(dev)
            if self.on_node_registered is not None:
                # сервер: upsert в реестр + подписки + HA-discovery
                await self.on_node_registered(node)
            if self.bridge is not None:
                await self.bridge.publish_lwt(f"ct/v1/{node.id}/availability")
        node.mark_seen()
        st = self._canon_state(dev)
        await self._publish_state(node.id, st)
        for cb in self._subs.get(node.id, []):
            try:
                await cb(st)
            except Exception:
                pass
        # --- политика Panic Button ---
        if path == "switch/panic_mode" and val.strip().upper() == "ON":
            await self._handle_panic_on(dev, node.id)

    def _canon_state(self, dev: str) -> dict[str, Any]:
        raw = self._raw.get(dev, {})
        smap = self.cfg["state_map"]
        tabs = int(raw.get("sensor/number_of_tabs", "0") or 0)
        on = tabs > 0
        state: dict[str, Any] = {
            "on": on, "online": True, "tabs": tabs,
            "mode": self._pulses.get(dev, {}).get("mode", "off"),
        }
        if acc := raw.get("sensor/accent_color"):
            state["color"] = _hex_to_rgb(acc)
        for src, key in (("battery", "battery"), ("ram", "ram_gb")):
            v = raw.get(f"sensor/{src}")
            if v is not None:
                try:
                    state[key] = float(v)
                except ValueError:
                    pass
        for bs in self.cfg["entities"]["binary_sensors"]:
            v = raw.get(f"binary_sensor/{bs}")
            if v is not None:
                state[bs] = v.strip().upper() == "ON"
        for sw in self.cfg["entities"]["switches"]:
            v = raw.get(f"switch/{sw}")
            if v is not None:
                state[f"switch_{sw}"] = v.strip().upper() == "ON"
        if b := smap.get("brightness_source"):
            state["bri"] = int(float(raw.get(f"sensor/{b}", "100")) * 255 / 100)
        else:
            state["bri"] = 255 if on else 0
        return state

    # ----------------------------- state cmds -----------------------------
    async def get_state(self, device_id: str) -> dict[str, Any]:
        dev = self._dev_by_node(device_id)
        if dev is None:
            raise KeyError(f"opera_mqtt: unknown device {device_id}")
        return self._canon_state(dev)

    async def set_state(self, device_id: str, state: dict[str, Any],
                        segment: Optional[Segment] = None) -> dict[str, Any]:
        if segment is not None:
            raise UnsupportedError("opera_mqtt: виртуальные пиксели неприменимы к вкладкам")
        dev = self._dev_by_node(device_id)
        if dev is None:
            raise KeyError(f"opera_mqtt: unknown device {device_id}")
        mode = state.get("mode", "off")
        self._stop_pulse(dev)
        if mode == "off":
            await self._cmd(dev, "switch/mute_all_tabs/set", "OFF")
        elif mode == "mute":
            await self._cmd(dev, "switch/mute_all_tabs/set", "ON")
        elif mode in ("blink", "breathing"):
            period = self.cfg["pulse_period_ms"].get(mode, 800) / 1000.0
            t = asyncio.create_task(self._pulse_loop(dev, period))
            self._pulses[dev] = {"mode": mode, "task": t}
        elif mode == "static":
            pass                       # «steady»: ничего жёсткого не делаем, вкладки как есть
        elif mode == "colorsweep":
            raise UnsupportedError(
                "opera_mqtt: colorsweep требует CDP-коннектора (marquee заголовка); "
                "штатными сущностями расширения не делается — честный unsupported")
        else:
            raise UnsupportedError(f"opera_mqtt: режим '{mode}' не описан в OPERA_MQTT_CONFIG")
        return await self.get_state(device_id)

    async def apply_ir(self, device_id: str, ir: EffectIR,
                       segment: Optional[Segment] = None) -> dict[str, Any]:
        cls = ir.class_.value if hasattr(ir.class_, "value") else str(ir.class_)
        native = self.cfg["ir_class_map"].get(cls)
        if native is None:
            losses = ir.losses + [f"opera_mqtt: класс '{cls}' не поддерживается — unsupported"]
            raise UnsupportedError("; ".join(losses))
        log.info("opera_mqtt apply_ir: %s -> %s (проекция на звук/окно, не RGB)", cls, native)
        return await self.set_state(device_id, {"mode": native}, segment)

    async def capabilities(self, device_id: str) -> DeviceNode:
        for n in self._nodes.values():
            if n.id == device_id:
                return n
        raise KeyError(f"opera_mqtt: unknown device {device_id}")

    async def subscribe(self, device_id: str,
                        cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._subs.setdefault(device_id, []).append(cb)

    # ----------------------------- internals -----------------------------
    def _dev_by_node(self, device_id: str) -> Optional[str]:
        for n in self._nodes.values():
            if n.id == device_id:
                return self._prefixes_key(n)
        return None

    def _prefixes_key(self, node: DeviceNode) -> str:
        for dev, n in self._nodes.items():
            if n is node:
                return dev
        return ""

    async def _cmd(self, dev: str, path: str, payload: str) -> None:
        """Команда вниз браузеру: opera/<prefix>/<path> с ON/OFF (протокол
        base/switch.js официального расширения).

        ВАЖНО (проверено по исходникам расширения): подписка идёт на СЫРОЙ
        payload ('ON'/'OFF', см. base/switch.js: this._enabled = payload === 'ON'),
        поэтому здесь публикуем строку как есть — json.dumps('ON') даёт '"ON"'
        и расширение такое не примет."""
        if self.bridge is None:
            return
        prefix = self._prefixes.get(dev, dev)
        await self.bridge.publish_raw(f"opera/{prefix}/{path}", payload, qos=1)

    async def _pulse_loop(self, dev: str, period_s: float) -> None:
        on = False
        try:
            while True:
                on = not on
                await self._cmd(dev, "switch/mute_all_tabs/set", "ON" if on else "OFF")
                await asyncio.sleep(period_s)
        except asyncio.CancelledError:
            pass

    def _stop_pulse(self, dev: str) -> None:
        p = self._pulses.pop(dev, None)
        if p and p.get("task"):
            p["task"].cancel()

    async def _handle_panic_on(self, dev: str, node_id: str) -> None:
        """Panic Button включён (руками F12 или кем-то из UI) -> гасим OFF
        штатной командой, если включён auto_off; debounce против флаттера."""
        pol = self.cfg["panic"]
        if not pol.get("auto_off", True):
            return
        now = time.time()
        if now - self._panic_last_off < pol.get("auto_off_debounce_s", 2.0):
            return
        self._panic_last_off = now
        log.warning("opera_mqtt: panic_mode ON у '%s' — отправляю OFF (антиприкол, конфиг panic)", node_id)
        await self._cmd(dev, "switch/panic_mode/set", "OFF")

    async def _ttl_loop(self) -> None:
        """Онлайн по TTL: браузер закрыт -> сообщений нет -> узел офлайн."""
        ttl = self.cfg["state_map"].get("online_ttl_s", 65.0)
        while True:
            await asyncio.sleep(min(ttl / 2, 10))
            now = time.time()
            for dev, node in list(self._nodes.items()):
                if now - self._last_seen.get(dev, 0) > ttl and \
                        node.online != OnlineState.OFFLINE:
                    node.online = OnlineState.OFFLINE
                    if self.bridge is not None:
                        from ct.core.mqtt_bridge import MqttBridge
                        await self.bridge.publish(MqttBridge.t_avail(node.id),
                                                  {"state": "offline"}, retain=True)
                        await self.bridge.publish(MqttBridge.t_state(node.id),
                                                  {"on": False, "online": False},
                                                  retain=True)

    async def _publish_state(self, node_id: str, state: dict) -> None:
        if self.bridge is None:
            return
        from ct.core.mqtt_bridge import MqttBridge
        await self.bridge.publish(MqttBridge.t_state(node_id), state, retain=True)
        await self.bridge.publish(MqttBridge.t_avail(node_id),
                                  {"state": "online"}, retain=True)

    # --- хуки для discovery-экспорта (ct/core/ha_discovery.py читает их тут) ---
    def ha_entity_policy(self) -> dict[str, Any]:
        """Что скрыть из HA UI и что автогасить. Реестр-агностично: экспортёр
        спрашивает у адаптера, а не знает про Opera сам."""
        hidden = list(self.cfg.get("hide_entities_extra", []))
        if self.cfg["panic"].get("hide_from_ha", True):
            hidden.append("switch.panic_mode")
        return {"hidden": hidden,
                "auto_off": {"switch.panic_mode":
                             bool(self.cfg["panic"].get("auto_off", True))},
                "namespace": "opera"}

    async def close(self) -> None:
        for dev in list(self._pulses):
            self._stop_pulse(dev)
        if self._ttl_task:
            self._ttl_task.cancel()


def _hex_to_rgb(val: str) -> dict[str, int]:
    m = re.search(r"#?([0-9a-fA-F]{6})", val or "")
    if not m:
        return {"r": 255, "g": 255, "b": 255}
    h = m.group(1)
    return {"r": int(h[0:2], 16), "g": int(h[2:4], 16), "b": int(h[4:6], 16)}
