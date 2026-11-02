"""Адаптер GyverHub-семейства (GyverMatrix, GyverLamp, гирлянда v3).

Транспорт: MQTT-топики схемы GyverHub + HTTP-эндпоинты где имеются.
Схема GyverHub (уточняется под конкретные прошивки, маппится на канон ct/v1):
  <base>/set            — команда (JSON или ключ=значение)
  <base>/state          — состояние (retain)
  <base>/available      — LWT online/offline
  <base>/config         — конфигурация узла
Generic-вариант для «прочей лапши» — см. generic.py.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

from ct.adapters.base import SouthboundAdapter, UnsupportedError
from ct.core.ir import EffectIR, NativeRef, Segment, Topology
from ct.core.registry import (DeviceNode, Endpoint, Ecosystem, OnlineState,
                              TransportKind)

GYVER_TO_CANON = {  # поля состояния GyverHub -> канонические имена
    "power": "on",
    "bright": "brightness",
    "mode": "effect",
    "speed": "speed",
    "pal": "palette",
}
CANON_TO_GYVER = {v: k for k, v in GYVER_TO_CANON.items()}


class GyverHubAdapter(SouthboundAdapter):
    name = "gyverhub"
    ecosystem = "gyverhub"

    def __init__(self, base_topics: dict[str, str], bridge, http_hosts: Optional[dict[str, str]] = None):
        """base_topics: {device_id: 'gyvermatrix/room1'}; http_hosts — где HTTP имеется."""
        self.base_topics = base_topics
        self.bridge = bridge           # MqttBridge к ЛОКАЛЬНОМУ брокеру
        self.http_hosts = http_hosts or {}
        self._nodes: dict[str, DeviceNode] = {}
        self._subs: dict[str, Callable] = {}

    async def discover(self) -> list[DeviceNode]:
        """Дискавери по брокеру: подписка на <prefix>/+/available и $SYS."""
        found = []
        prefix = next(iter(self.base_topics.values()), "").split("/")[0]
        event = asyncio.Event()
        seen: dict[str, dict] = {}

        async def _probe(topic: str, payload: dict) -> None:
            parts = topic.split("/")
            dev = parts[-2] if len(parts) >= 2 else topic
            seen[dev] = payload
            event.set()

        self.bridge.on(f"{prefix}/+/available", _probe)
        try:
            await asyncio.wait_for(event.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        for dev in seen:
            base_topic = self.base_topics.get(dev, f"{prefix}/{dev}")
            node = DeviceNode(id=dev, name=dev, ecosystem=Ecosystem.GYVERHUB,
                              adapter=self.name,
                              topology=Topology(kind="matrix", length=0, width=16, height=16),
                              endpoints=[Endpoint(kind=TransportKind.MQTT, base_topic=base_topic)])
            if dev in self.http_hosts:
                node.endpoints.append(Endpoint(kind=TransportKind.HTTP, url=self.http_hosts[dev]))
            self._nodes[dev] = node
            found.append(node)
        return found

    async def capabilities(self, device_id: str) -> DeviceNode:
        node = self._nodes[device_id]
        bt = node.endpoints[0].base_topic
        cfg = await self._request(f"{bt}/config")
        if isinstance(cfg, dict):
            w = int(cfg.get("width", cfg.get("w", 0)) or 0)
            h = int(cfg.get("height", cfg.get("h", 0)) or 0)
            n = int(cfg.get("count", cfg.get("num", 0)) or 0)
            if w and h:
                node.topology = Topology(kind="matrix", width=w, height=h, length=w * h)
            elif n:
                node.topology = Topology(kind="strip", length=n)
            node.native_effects = [NativeRef(ecosystem=self.ecosystem, effect=e)
                                   for e in cfg.get("modes", [])]
        node.mark_seen()
        return node

    async def get_state(self, device_id: str) -> dict[str, Any]:
        bt = self._base(device_id)
        raw = await self._request(f"{bt}/state")
        canon = {GYVER_TO_CANON.get(k, k): v for k, v in (raw or {}).items()}
        await self.bridge.publish(self.bridge.t_state(device_id), canon, retain=True)
        return canon

    async def set_state(self, device_id: str, state: dict[str, Any],
                        segment: Optional[Segment] = None) -> dict[str, Any]:
        bt = self._base(device_id)
        gy = {CANON_TO_GYVER.get(k, k): v for k, v in state.items() if k != "sync"}
        if segment is not None:
            # виртуальные пиксели: у Gyver-прошивок сегментации нет в общем виде —
            # уходит как offset/length, прошивка native_ct-форка их понимает;
            # для стоковых — игнорируется с декларацией потери на уровне сервера.
            gy["offset"] = segment.start
            gy["length"] = segment.length
        await self.bridge.publish(f"{bt}/set", gy, retain=True)
        return {"published": f"{bt}/set", "payload": gy}

    async def apply_ir(self, device_id: str, ir: EffectIR,
                       segment: Optional[Segment] = None) -> dict[str, Any]:
        raise UnsupportedError("GyverHub stock firmware needs IR->native translation")

    async def subscribe(self, device_id: str,
                        cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        bt = self._base(device_id)

        async def _h(topic: str, payload: dict) -> None:
            if topic.endswith("/available"):
                node = self._nodes.get(device_id)
                if node:
                    if payload.get("state") == "offline" or payload in ({"online": False}, {"state": "offline"}):
                        node.online = OnlineState.OFFLINE
                    else:
                        node.mark_seen()
            canon = {GYVER_TO_CANON.get(k, k): v for k, v in payload.items()} \
                if isinstance(payload, dict) else payload
            await cb(canon)

        self.bridge.on(f"{bt}/+", _h)
        self.bridge.on(self.bridge.t_avail(device_id), _h)

    def _base(self, device_id: str) -> str:
        return self._nodes[device_id].endpoints[0].base_topic

    async def _request(self, topic: str, timeout: float = 3.0) -> Optional[dict]:
        """Запрос состояния через retained-сообщение: подписка + ожидание."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        async def _once(t: str, p: dict) -> None:
            if not fut.done():
                fut.set_result(p)

        self.bridge.on(topic, _once)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
