"""Capability registry — каноническая модель устройства.

Все решения о портировании принимаются на основе DeviceNode:
топология, эндпоинты транспорта, родные эффекты, онлайн-статус (LWT/retain).
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from ct.core.ir import NativeRef, Topology


class Ecosystem(str, Enum):
    WLED = "wled"
    GYVERHUB = "gyverhub"     # GyverMatrix / GyverLamp / гирлянда v3
    GENERIC = "generic"       # HTTP/MQTT/UDP + конфиг маппинга
    NATIVE_CT = "native_ct"   # собственная прошивка-IR-интерпретатор


class TransportKind(str, Enum):
    HTTP = "http"
    WS = "ws"
    MQTT = "mqtt"
    UDP = "udp"


class Endpoint(BaseModel):
    kind: TransportKind
    url: Optional[str] = None            # http://192.168.1.40
    base_topic: Optional[str] = None     # gyvermatrix/room1
    port: Optional[int] = None


class OnlineState(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class DeviceNode(BaseModel):
    """Канонический узел. Единая схема для всех экосистем."""

    id: str
    name: str = ""
    ecosystem: Ecosystem
    adapter: str                          # имя класса адаптера ("wled", "gyverhub", ...)
    topology: Topology
    endpoints: list[Endpoint] = Field(default_factory=list)
    native_effects: list[NativeRef] = Field(default_factory=list)
    supports_ir: bool = False             # прошивка умеет есть EffectIR JSON?
    supports_segments: bool = False        # принимает виртуальные пиксели (start/length)
    telemetry_kinds: list[str] = Field(default_factory=list)  # ["audio_level", ...]
    online: OnlineState = OnlineState.UNKNOWN
    last_seen: float = 0.0
    fw_version: str = ""
    labels: dict[str, str] = Field(default_factory=dict)

    def mark_seen(self) -> None:
        self.online = OnlineState.ONLINE
        self.last_seen = time.time()

    def effect_names(self) -> set[str]:
        return {e.effect.lower() for e in self.native_effects}


class Registry:
    """Реестр устройств + подписка на изменения (для WebSocket/CLI)."""

    def __init__(self) -> None:
        self._devices: dict[str, DeviceNode] = {}
        self._listeners: list = []

    def upsert(self, node: DeviceNode) -> DeviceNode:
        self._devices[node.id] = node
        self._notify(node.id)
        return node

    def remove(self, device_id: str) -> None:
        self._devices.pop(device_id, None)

    def get(self, device_id: str) -> Optional[DeviceNode]:
        return self._devices.get(device_id)

    def all(self) -> list[DeviceNode]:
        return list(self._devices.values())

    def online(self) -> list[DeviceNode]:
        return [d for d in self._devices.values() if d.online == OnlineState.ONLINE]

    def subscribe(self, cb) -> None:
        self._listeners.append(cb)

    def _notify(self, device_id: str) -> None:
        for cb in self._listeners:
            try:
                cb(device_id)
            except Exception:
                pass
