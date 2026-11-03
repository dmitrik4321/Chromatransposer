"""Адаптер GyverHub — MQTT-конструктор UI (AlexGyver/GyverHub).

ВАЖНО (по реальным исходникам экосистемы): GyverHub — это фреймворк-конструктор
веб-морды на ESP, где топики MQTT задаёт пользователь в скетче прошивки.
Фиксированной схемы state/set/config НЕ СУЩЕСТВУЕТ: имена виджетов и топиков —
произвольные. Поэтому адаптер работает по YAML-маппингу (base_topics +
widget_map), а не по угаданной схеме:

  widgets: {канон_поле: имя_топика_виджета}   # напр. brightness: "lamp/bri"

Для стоковых прошивок семейства (GyverLamp2 и пр.) GyverHub не используется —
см. отдельный адаптер gyverlamp2.py (UDP «GL»).
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


# ------------------------------ КОНФИГ АДАПТЕРА ------------------------------
# Всё меняемое — здесь; переопределяется секцией adapters[].config в YAML.
GYVERHUB_CONFIG = {
    "retained_timeout_s": 3.0,   # ожидание retained-ответа на подписку
}


class GyverHubAdapter(SouthboundAdapter):
    name = "gyverhub"
    ecosystem = "gyverhub"

    def __init__(self, devices: Optional[dict[str, dict]] = None, bridge=None,
                 config: Optional[dict] = None):
        """devices: {device_id: {"topic_map": {canon: topic},
                                "avail_topic": "...", "topology": {...},
                                "effects": [...], "http": "..."}}.
        topic_map строится из скетча GyverHub пользователя (имена виджетов)."""
        self.cfg = {**GYVERHUB_CONFIG, **(config or {})}   # YAML поверх дефолтов
        self.devices_cfg = devices or {}
        self.bridge = bridge           # MqttBridge к ЛОКАЛЬНОМУ брокеру
        self._nodes: dict[str, DeviceNode] = {}
        self._subs: dict[str, Callable] = {}

    async def discover(self) -> list[DeviceNode]:
        """Дискавери по сконфигурированным устройствам (GyverHub сам ничего
        не анонсирует стандартными топиками — только по YAML-конфигу)."""
        found = []
        for dev, cfg in self.devices_cfg.items():
            topo = cfg.get("topology", {})
            node = DeviceNode(id=dev, name=cfg.get("name", dev),
                              ecosystem=Ecosystem.GYVERHUB, adapter=self.name,
                              topology=Topology(**topo) if topo else
                              Topology(kind="matrix", width=16, height=16, length=256),
                              endpoints=[Endpoint(kind=TransportKind.MQTT,
                                                  base_topic=next(iter(
                                                      cfg.get("topic_map", {}).values()),
                                                      dev))],
                              native_effects=[NativeRef(ecosystem=self.ecosystem, effect=e)
                                              for e in cfg.get("effects", [])])
            if cfg.get("http"):
                node.endpoints.append(Endpoint(kind=TransportKind.HTTP, url=cfg["http"]))
            self._nodes[dev] = node
            found.append(node)
        return found

    async def capabilities(self, device_id: str) -> DeviceNode:
        self._nodes[device_id].mark_seen()
        return self._nodes[device_id]

    async def get_state(self, device_id: str) -> dict[str, Any]:
        tm = self._topic_map(device_id)
        canon: dict[str, Any] = {}
        for field, topic in tm.items():
            val = await self._request(topic)
            if val is not None:
                canon[field] = val.get("value", val) if isinstance(val, dict) else val
        await self.bridge.publish(self.bridge.t_state(device_id), canon, retain=True)
        return canon

    async def set_state(self, device_id: str, state: dict[str, Any],
                        segment: Optional[Segment] = None) -> dict[str, Any]:
        tm = self._topic_map(device_id)
        sent: dict[str, Any] = {}
        for k, v in state.items():
            if k == "sync" or k == "segment":
                continue
            topic = tm.get(k)
            if topic is None:
                sent[f"_unmapped:{k}"] = v
                continue
            await self.bridge.publish(topic, v, retain=False)
            sent[k] = v
        if segment is not None:
            # у GyverHub-прошивок сегментации нет, если её не заложил автор скетча;
            # уходит offset/length отдельным топиком, если замапплен, иначе теряется явно
            stopic = tm.get("segment")
            if stopic:
                await self.bridge.publish(stopic, {"start": segment.start,
                                                   "length": segment.length})
            else:
                sent["_loss"] = "segment ignored: no widget mapping in sketch"
        sent.setdefault("_declared_unsupported", [k for k in sent if k.startswith("_unmapped:")])
        return sent

    async def apply_ir(self, device_id: str, ir: EffectIR,
                       segment: Optional[Segment] = None) -> dict[str, Any]:
        raise UnsupportedError("GyverHub-скетч сам решает, какие виджеты есть; "
                               "IR транслируется в нативные поля через topic_map + translator")

    async def subscribe(self, device_id: str,
                        cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        tm = self._topic_map(device_id)
        rev = {t: f for f, t in tm.items()}

        async def _h(topic: str, payload: dict) -> None:
            field = rev.get(topic, topic.rsplit("/", 1)[-1])
            node = self._nodes.get(device_id)
            if node:
                node.mark_seen()
            value = payload.get("value", payload) if isinstance(payload, dict) else payload
            await cb({field: value})

        for topic in tm.values():
            self.bridge.on(topic, _h)
        avail = self.devices_cfg[device_id].get("avail_topic")
        if avail:
            async def _a(topic: str, payload: dict) -> None:
                node = self._nodes.get(device_id)
                if node:
                    if isinstance(payload, dict) and payload.get("state") == "offline":
                        node.online = OnlineState.OFFLINE
                    else:
                        node.mark_seen()
            self.bridge.on(avail, _a)

    def _topic_map(self, device_id: str) -> dict[str, str]:
        return dict(self.devices_cfg.get(device_id, {}).get("topic_map", {}))

    async def _request(self, topic: str, timeout: Optional[float] = None) -> Optional[dict]:
        """Запрос состояния через retained-сообщение: подписка + ожидание."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        async def _once(t: str, p: dict) -> None:
            if not fut.done():
                fut.set_result(p)

        self.bridge.on(topic, _once)
        try:
            return await asyncio.wait_for(fut,
                                          timeout or self.cfg["retained_timeout_s"])
        except asyncio.TimeoutError:
            return None
