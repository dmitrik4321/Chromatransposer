"""Адаптер собственной прошивки native_ct (ESP32, см. firmware/).

Транспорт: MQTT (каноническое дерево ct/v1) и/или HTTP. Прошивка:
 - принимает EffectIR JSON в ct/v1/<id>/ir;
 - принимает сегментацию (виртуальные пиксели) в поле segment;
 - принимает sync tick (tick_seq/wall_ts) в ct/v1/sync/tick;
 - публикует телеметрию (звук и т.п.) в ct/v1/<id>/telemetry/<kind>;
 - LWT available + retain state.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from ct.adapters.base import SouthboundAdapter
from ct.core.ir import EffectClass, EffectIR, NativeRef, Segment, Topology
from ct.core.registry import (DeviceNode, Endpoint, Ecosystem, OnlineState,
                              TransportKind)


# ------------------------------ КОНФИГ АДАПТЕРА ------------------------------
# Всё меняемое — здесь; переопределяется секцией adapters[].config в YAML.
NATIVE_CT_CONFIG = {
    "state_retain": True,           # retain для state/available (детекция офлайна)
    "set_qos": 1,                   # команды с гарантией доставки
    "sensor_forward_qos": 0,        # телеметрия — поспешнее, без гарантий
}


class NativeCtAdapter(SouthboundAdapter):
    name = "native_ct"
    ecosystem = "native_ct"

    def __init__(self, devices: dict[str, dict], bridge, config: Optional[dict] = None):
        """devices: {device_id: {"topology": {...}, "telemetry": ["audio_level"]}} из конфига."""
        self.cfg = {**NATIVE_CT_CONFIG, **(config or {})}   # YAML поверх дефолтов
        self.cfgs = devices
        self.bridge = bridge
        self._nodes: dict[str, DeviceNode] = {}

    async def discover(self) -> list[DeviceNode]:
        found = []
        for dev_id, cfg in self.cfgs.items():
            node = DeviceNode(
                id=dev_id, name=cfg.get("name", dev_id),
                ecosystem=Ecosystem.NATIVE_CT, adapter=self.name,
                topology=Topology(**cfg.get("topology", {"kind": "strip", "length": 0})),
                supports_ir=True, supports_segments=True,
                telemetry_kinds=cfg.get("telemetry", []),
                native_effects=[NativeRef(ecosystem=self.ecosystem, effect=c.value)
                                for c in EffectClass],
                endpoints=[Endpoint(kind=TransportKind.MQTT, base_topic=f"ct/v1/{dev_id}")],
            )
            self._nodes[dev_id] = node
            found.append(node)
            # подписка на availability
            async def _avail(t: str, p: dict, n=node) -> None:
                if p.get("state") == "offline":
                    n.online = OnlineState.OFFLINE
                else:
                    n.mark_seen()
            self.bridge.on(self.bridge.t_avail(dev_id), _avail)
        return found

    async def capabilities(self, device_id: str) -> DeviceNode:
        return self._nodes[device_id]

    async def get_state(self, device_id: str) -> dict[str, Any]:
        st = await self._retained(self.bridge.t_state(device_id))
        return st or {}

    async def set_state(self, device_id: str, state: dict[str, Any],
                        segment: Optional[Segment] = None) -> dict[str, Any]:
        payload = dict(state)
        if segment is not None:
            payload["segment"] = segment.model_dump()
        await self.bridge.publish(self.bridge.t_set(device_id), payload,
                                  qos=self.cfg["set_qos"])
        return payload

    async def apply_ir(self, device_id: str, ir: EffectIR,
                       segment: Optional[Segment] = None) -> dict[str, Any]:
        # by_alias=True — поле сериализуется как "class", как ожидает прошивка
        payload = ir.model_dump(by_alias=True, exclude_none=True)
        if segment is not None:
            payload["segment"] = segment.model_dump()
        await self.bridge.publish(self.bridge.t_ir(device_id), payload,
                                  qos=self.cfg["set_qos"])
        return payload

    async def subscribe(self, device_id: str,
                        cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        async def _h(topic: str, payload: dict) -> None:
            await cb({"topic": topic, **payload})
        self.bridge.on(self.bridge.t_state(device_id), _h)
        self.bridge.on(self.bridge.t_telemetry(device_id, "#"), _h)

    async def _retained(self, topic: str):
        fut = asyncio.get_running_loop().create_future()
        async def _once(t, p):
            if not fut.done():
                fut.set_result(p)
        self.bridge.on(topic, _once)
        try:
            return await asyncio.wait_for(fut, 2)
        except asyncio.TimeoutError:
            return None
