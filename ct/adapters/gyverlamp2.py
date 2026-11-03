"""Southbound-адаптер GyverLamp2 — стоковая прошивка AlexGyver/GyverLamp2.

WiFi-контроль лампы реализован НЕ через MQTT (в публичной прошивке MQTT-поля
конфигурации не образуют рабочей интеграции), а через UDP-протокол «GL»,
расписанный сообществом в smeleshkin/GyverLamp2HA (docs/protocol.md).
Адаптер повторяет проверенное поведение HA-интеграции:

  * unicast команд на IP лампы (чтобы не задевать другие лампы группы);
  * слушает broadcast heartbeat GL_ONL<n> на вычисленном порту (группа+ключ);
  * сериализует отправки с интервалом >=650 мс (throttle прошивки 500 мс),
    без 6-кратных дублей как в Android-приложении;
  * пишет режимы только полным банком GL,2 (декларированная семантика);
  * ACK в протоколе нет — reported-state подтверждается heartbeat'ом.

Ограничения, которые мы честно декларируем в capabilities:
  * сегментации виртуальных пикселей нет (1 лампа = 1 кольцо/лента);
  * soundReact встроен, но телеметрия звука наружу не отдаётся;
  * GL,1 (полный конфиг), будильники и пользовательские палитры — не трогаем.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from ct.adapters import gyverlamp2_protocol as gl
from ct.adapters.base import SouthboundAdapter, UnsupportedError
from ct.core.ir import EffectIR, NativeRef, Segment, Topology
from ct.core.registry import (DeviceNode, Endpoint, Ecosystem, OnlineState,
                              TransportKind)

log = logging.getLogger("ct.adapter.gyverlamp2")

# ------------------------------ КОНФИГ АДАПТЕРА ------------------------------
# Всё меняемое — здесь; сервер переопределяет любой ключ секцией
# adapters[].config в configs/server.yaml (GyverLamp2Adapter(config=...)).
GYVERLAMP2_CONFIG = {
    "heartbeat_timeout_s": 3.0,   # offline-детекция: heartbeat каждые ~500 мс
    "default_group": 1,           # группа UDP «GL» (порт выводится из group+key)
    "default_key": "GL",
    # IR-класс -> effect id прошивки (только то, что реально есть в Glade-прошивке;
    # неизвестные классы — честный unsupported, молчаливой подмены нет).
    # Ключи = значения EffectClass (ct/core/ir.py), ids = FirmwareEffect из протокола.
    "ir_class_map": {
        "perlin": 1,          # PERLIN
        "static": 2,          # COLOR == static с фиксированным оттенком
        "colorsweep": 3,      # CYCLE
        "gradient": 4,        # GRADIENT
        "twinkle": 5,         # PARTICLES ≈ мерцание (потеря декларируется)
        "fire": 6,            # FIRE
        "rainbow": 3,         # радуга циклом палитры rainbow (lossy)
    },
    # дефолты банка режимов GL,2 (полная запись — декларированная семантика)
    "mode_defaults": {
        "brightness_byte": 255, "speed_byte": 128, "scale_byte": 128,
        "adv_mode": 1, "sound_react": 1, "react_min": 0, "react_max": 255,
        "fade_bright": 0,
    },
}
# ------------------------------------------------------------------------------

CLASS_TO_FIRMWARE: dict[str, int] = GYVERLAMP2_CONFIG["ir_class_map"]


class _UdpHeartbeatProtocol(asyncio.DatagramProtocol):
    def __init__(self, bus: "GyverLamp2Adapter"):
        self.bus = bus

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.bus._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.warning("UDP error: %s", exc)


class GyverLamp2Adapter(SouthboundAdapter):
    name = "gyverlamp2"
    ecosystem = "gyverlamp2"

    def __init__(self, lamps: Optional[dict[str, dict]] = None, bridge=None,
                 group: Optional[int] = None, key: Optional[str] = None,
                 listen: bool = True, config: Optional[dict] = None):
        """lamps: {device_id: {"ip": "192.168.1.51", "group": 1?, "key": "GL"?}}."""
        self.cfg = {**GYVERLAMP2_CONFIG, **(config or {})}   # YAML поверх дефолтов
        self.cfg["ir_class_map"] = {**GYVERLAMP2_CONFIG["ir_class_map"],
                                    **((config or {}).get("ir_class_map") or {})}
        self.lamp_cfg = lamps or {}
        self.bridge = bridge
        self.group = group if group is not None else self.cfg["default_group"]
        self.key = key if key is not None else self.cfg["default_key"]
        self.listen = listen
        self._nodes: dict[str, DeviceNode] = {}
        self._last_preset: dict[str, int] = {}
        self._subs: dict[str, list[Callable]] = {}
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._send_lock = asyncio.Lock()
        self._last_send_ts = 0.0

    # --- транспорт -------------------------------------------------------------
    async def _ensure_bus(self) -> None:
        if self._transport is not None or not self.listen:
            return
        port = gl.udp_port(self.group, self.key)
        loop = asyncio.get_running_loop()
        self._bus_port = port
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpHeartbeatProtocol(self),
            local_addr=("0.0.0.0", port), allow_broadcast=True)
        log.info("GyverLamp2 UDP bus listening on :%d (group=%d)", port, self.group)

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        preset = gl.parse_heartbeat(data)
        if preset is None:
            return
        dev = next((d for d, n in self._nodes.items()
                    if n.endpoints and n.endpoints[0].url.endswith(addr[0])), None)
        if dev is None:
            return
        node = self._nodes[dev]
        node.mark_seen()
        changed = self._last_preset.get(dev) != preset
        self._last_preset[dev] = preset
        state = {"on": True, "preset": preset,
                 "effect": gl.EFFECT_KEYS.get(preset % len(gl.EFFECT_KEYS), f"preset{preset}")}
        asyncio.ensure_future(self._dispatch(dev, state)) if changed else None

    async def _dispatch(self, device_id: str, state: dict) -> None:
        for cb in self._subs.get(device_id, []):
            try:
                await cb(state)
            except Exception:
                log.exception("subscriber error")
        if self.bridge is not None:
            try:
                await self.bridge.publish(self.bridge.t_state(device_id), state, retain=True)
                await self.bridge.publish(self.bridge.t_avail(device_id),
                                          {"state": "online"}, retain=True)
            except Exception:
                pass

    async def _send(self, device_id: str, packet: bytes) -> None:
        node = self._nodes[device_id]
        host = node.endpoints[0].url.rsplit("/", 1)[-1]
        if self._transport is None:
            raise RuntimeError("UDP bus не запущен (listen=False?)")
        # сериализация: throttle прошивки 500 мс, берём 650 мс между отправками
        async with self._send_lock:
            wait = gl.SEND_MIN_INTERVAL - (time.monotonic() - self._last_send_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            port = getattr(self, "_bus_port", gl.udp_port(self.group, self.key))
            self._transport.sendto(packet, (host, port))
            self._last_send_ts = time.monotonic()

    # --- контракт адаптера -------------------------------------------------------
    async def discover(self) -> list[DeviceNode]:
        await self._ensure_bus()
        found = []
        for dev, cfg in self.lamp_cfg.items():
            group = int(cfg.get("group", self.group))
            key = str(cfg.get("key", self.key))
            node = DeviceNode(
                id=dev, name=cfg.get("name", dev), ecosystem=Ecosystem.GYVERLAMP2,
                adapter=self.name, fw_version="stock (GL_VERSION 23 по данным сообщества)",
                topology=Topology(kind=cfg.get("topology", "strip"),
                                  length=int(cfg.get("ledcount", 0)),
                                  width=1, height=1),
                endpoints=[Endpoint(kind=TransportKind.UDP, url=f"udp://{cfg['ip']}",
                                    port=gl.udp_port(group, key))],
                native_effects=[NativeRef(ecosystem=self.ecosystem, effect=k)
                                for k in gl.EFFECT_KEYS.values()],
                supports_ir=False, supports_segments=False,
                telemetry_kinds=[],  # звук внутрь, наружу не отдаётся
                labels={"gl_group": str(group), "gl_key": key},
            )
            self._nodes[dev] = node
            found.append(node)
        return found

    async def capabilities(self, device_id: str) -> DeviceNode:
        node = self._nodes[device_id]
        node.online = (OnlineState.ONLINE
                       if time.time() - node.last_seen < self.cfg["heartbeat_timeout_s"]
                       else OnlineState.UNKNOWN)
        return node

    async def get_state(self, device_id: str) -> dict[str, Any]:
        """В прошивке нет read-back: состояние реконструируется из heartbeat."""
        preset = self._last_preset.get(device_id)
        node = self._nodes[device_id]
        online = node.online == OnlineState.ONLINE
        return {"on": online and preset is not None, "preset": preset,
                "reachable": online}

    async def set_state(self, device_id: str, state: dict[str, Any],
                        segment: Optional[Segment] = None) -> dict[str, Any]:
        if segment is not None:
            raise UnsupportedError("GyverLamp2 stock: виртуальные пиксели не поддерживаются; "
                                   "для сегментации нужен native_ct-форк")
        if state.get("effect") and "preset" not in state:
            # named-эффект -> слот банка режимов см. apply_mode_bank
            return await self._apply_named_effect(device_id, state)
        if "on" in state:
            cmd = gl.ControlCommand.ON if state["on"] else gl.ControlCommand.OFF
            await self._send(device_id, gl.control_packet(cmd))
        if "brightness" in state:
            bri = int(round(float(state["brightness"]) * 255)) \
                if float(state["brightness"]) <= 1.0 else int(state["brightness"])
            if bri <= 0:
                await self._send(device_id, gl.control_packet(gl.ControlCommand.OFF))
            else:
                await self._send(device_id, gl.slave_brightness(max(1, min(255, bri))))
        if "preset" in state:
            await self._send(device_id, gl.slave_preset(int(state["preset"])))
        rep = await self.get_state(device_id)
        await self._dispatch(device_id, rep)
        return rep

    async def _apply_named_effect(self, device_id: str, state: dict) -> dict[str, Any]:
        """Эффект по имени: пишем банк режимов GL,2 (полная замена, 10 слотов)
        и выбираем слот. Это декларированная запись — не молчаливая подмена.
        Дефолты полей режима — в GYVERLAMP2_CONFIG['mode_defaults'] (YAML)."""
        name = str(state["effect"]).lower()
        fid = gl.KEY_TO_EFFECT.get(name)
        if fid is None:
            raise UnsupportedError(f"GyverLamp2: неизвестный родной эффект '{name}'")
        d = self.cfg["mode_defaults"]
        row = [0] * gl.MODE_FIELDS
        row[gl.ModeField.EFFECT] = fid
        row[gl.ModeField.FADE_BRIGHT] = d["fade_bright"]
        row[gl.ModeField.BRIGHTNESS] = int(state.get("brightness_byte", d["brightness_byte"]))
        row[gl.ModeField.ADV_MODE] = d["adv_mode"]
        row[gl.ModeField.SOUND_REACT] = d["sound_react"]
        row[gl.ModeField.REACT_MIN] = d["react_min"]
        row[gl.ModeField.REACT_MAX] = d["react_max"]
        row[gl.ModeField.SPEED] = int(state.get("speed_byte", d["speed_byte"]))
        row[gl.ModeField.PALETTE] = gl.palette_id(state.get("palette", "rainbow"))
        row[gl.ModeField.SCALE] = int(state.get("scale_byte", d["scale_byte"]))
        bank = [tuple(row)]
        await self._send(device_id, gl.mode_bank_packet(bank, selected=0))
        rep = {"on": True, "effect": name}
        await self._dispatch(device_id, rep)
        return rep

    async def apply_ir(self, device_id: str, ir: EffectIR,
                       segment: Optional[Segment] = None) -> dict[str, Any]:
        """IR -> банк режимов прошивки. Нет класса — честный unsupported
        (см. также ct.core.translator для деградационных композиций)."""
        if segment is not None:
            raise UnsupportedError("GyverLamp2: сегментация недоступна")
        fid = CLASS_TO_FIRMWARE.get(ir.class_.value)
        if fid is None:
            raise UnsupportedError(
                f"GyverLamp2: класс эффекта '{ir.class_.value}' отсутствует в прошивке; "
                f"доступны: {sorted(CLASS_TO_FIRMWARE)}")
        losses = list(ir.losses)
        pal_id = self._map_palette(ir, losses)
        state = {
            "on": True,
            "effect": gl.EFFECT_KEYS[fid],
            "brightness_byte": max(1, min(255, int(ir.params.brightness * 255))),
            "speed_byte": max(0, min(255, int(ir.params.speed * 255))),
            "scale_byte": max(0, min(255, int(ir.params.scale * 255))),
            "palette": pal_id,
        }
        res = await self._apply_named_effect(device_id, state)
        res["losses"] = losses
        return res

    @staticmethod
    def _map_palette(ir: EffectIR, losses: list[str]) -> int:
        """Палитра IR (стопы) -> один из встроенных id 2..32. Точного совпадения
    градиентов нет — сопоставляем по имени или декларируем потерю на rainbow."""
        name = (ir.palette.name or "").lower().replace(" ", "_")
        try:
            return gl.palette_id(name)
        except ValueError:
            losses.append(f"palette '{ir.palette.name or 'custom'}' не сопоставлена — "
                          "использована rainbow (декларированная потеря)")
            return gl.RAINBOW_PALETTE_ID

    async def subscribe(self, device_id: str,
                        cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._subs.setdefault(device_id, []).append(cb)

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
