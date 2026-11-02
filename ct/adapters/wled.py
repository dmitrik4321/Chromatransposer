"""Адаптер WLED: HTTP JSON API (/json) + WebSocket (/ws), если есть в сборке.

Маппинг родных сущностей в каноническое дерево ct/v1/...
Родные шкалы: speed/intensity/palette_length 0..255, яркость bri 0..255.
Сегменты: WLED умеет /json/segments[i] — ложимся на это для виртуальных пикселей.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

from ct.adapters.base import SouthboundAdapter, UnsupportedError
from ct.core.ir import EffectIR, NativeRef, Segment, Topology
from ct.core.registry import (DeviceNode, Endpoint, Ecosystem, OnlineState,
                              TransportKind)

try:
    import aiohttp
except ImportError:  # aiohttp не критичен до первого реального запуска
    aiohttp = None


# ------------------------------ КОНФИГ АДАПТЕРА ------------------------------
# Всё меняемое собрано здесь; сервер может переопределить любой ключ через
# секцию adapters.wled в configs/server.yaml (см. WledAdapter.__init__).
WLED_CONFIG = {
    "http_timeout_s": 3.0,          # таймаут discovery/запросов
    "ws_reconnect_s": 5.0,          # пауза переподключения WebSocket
    "state_publish_ms": 2000,       # как часто читать /json/state для реконсиляции
    "scale_max": 255,               # родная шкала speed/intensity/palette/bri
    "paths": {                      # эндпоинты родного HTTP API WLED
        "info": "/json/info",
        "state": "/json/state",
        "effect": "/json/effects",
        "palette": "/json/palettes",
        "preset": "/wpt",           # GET/POST /wpt/<n>
        "ws": "/ws",
    },
    "ir_class_map": {               # EffectIR.class -> имя нативного эффекта WLED
        "perlin": "Noise 2",
        "plasma": "Plasma",
        "fire": "Firework",         # близкий класс; точный fire2020 — lossy
        "rain": "Rainstorm",
        "twinkle": "Twinklecat",
        "static": "Solid",
        "colorsweep": "Colorwave",
        "rainbow": "Rainbow",
        "sweep": "Scan",            # деградация: однопоточное сканирование
        # неизвестные классы НЕ маппим молча — см. UnsupportedError ниже
    },
}
# -----------------------------------------------------------------------------


class WledAdapter(SouthboundAdapter):
    name = "wled"
    ecosystem = "wled"

    def __init__(self, hosts: list[str], bridge=None, config: Optional[dict] = None):
        self.hosts = hosts          # из конфига; discovery дополняет через mDNS _wled._tcp
        self.bridge = bridge        # MqttBridge для нормализации топиков
        self.cfg = {**WLED_CONFIG, **(config or {})}   # YAML поверх дефолтов
        self._nodes: dict[str, DeviceNode] = {}

    async def discover(self) -> list[DeviceNode]:
        found = []
        if aiohttp is None:
            return found
        timeout = aiohttp.ClientTimeout(self.cfg["http_timeout_s"])
        async with aiohttp.ClientSession(timeout=timeout) as s:
            for host in self.hosts:
                try:
                    async with s.get(f"http://{host}{self.cfg['paths']['info']}") as r:
                        info = await r.json()
                    node = self._node_from_info(host, info)
                    self._nodes[node.id] = node
                    found.append(node)
                except Exception:
                    continue
        return found

    def _node_from_info(self, host: str, info: dict) -> DeviceNode:
        dev_id = f"wled-{info.get('id', host.replace('.', '-'))}"
        led = info.get("leds", [{}])[0] if isinstance(info.get("leds"), list) else {}
        count = int(led.get("count", 0)) or int(info.get("led", {}).get("count", 0))
        top = Topology(kind="strip", length=count, width=1, height=1,
                       color_order=led.get("color_order", "GRB"))
        return DeviceNode(id=dev_id, name=info.get("name", host),
                          ecosystem=Ecosystem.WLED, adapter=self.name,
                          topology=top, fw_version=str(info.get("ver", "")),
                          supports_ir=False, supports_segments=True,
                          endpoints=[Endpoint(kind=TransportKind.HTTP, url=f"http://{host}"),
                                     Endpoint(kind=TransportKind.WS,
                                              url=f"ws://{host}{self.cfg['paths']['ws']}")])

    async def capabilities(self, device_id: str) -> DeviceNode:
        node = self._nodes[device_id]
        url = node.endpoints[0].url
        if aiohttp is not None:
            timeout = aiohttp.ClientTimeout(self.cfg["http_timeout_s"])
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(f"{url}{self.cfg['paths']['state']}") as r:
                    st = await r.json()
                seg_count = len(st.get("segments", []))
                node.topology.length = sum(sg.get("len", 0) for sg in st.get("segments", [])) or node.topology.length
                node.supports_segments = seg_count > 0
        node.mark_seen()
        return node

    async def get_state(self, device_id: str) -> dict[str, Any]:
        url = self._url(device_id)
        timeout = aiohttp.ClientTimeout(self.cfg["http_timeout_s"])
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{url}{self.cfg['paths']['state']}") as r:
                raw = await r.json()
        canon = {
            "on": raw.get("on", False),
            "brightness": raw.get("bri", self.cfg["scale_max"]),
            "effect": (raw.get("cx") or ""),   # effect id/имя
            "palette": raw.get("ps", ""),
            "segment_count": len(raw.get("segments", [])),
        }
        if self.bridge:
            await self.bridge.publish(self.bridge.t_state(device_id), canon, retain=True)
        return canon

    async def set_state(self, device_id: str, state: dict[str, Any],
                        segment: Optional[Segment] = None) -> dict[str, Any]:
        url = self._url(device_id)
        body: dict[str, Any] = {}
        for k in ("on", "bri"):
            if k in state:
                body[k] = state[k]
        if segment is not None:
            # Виртуальный пиксель: пишем в конкретный сегмент WLED.
            body["seg"] = [{"id": 0, "start": segment.start,
                            "stop": segment.start + segment.length,
                            **{k: v for k, v in state.items()
                               if k in ("sx", "ix", "fx", "col", "pal")}}]
        elif "native" in state:
            body.update(state["native"])
        timeout = aiohttp.ClientTimeout(self.cfg["http_timeout_s"])
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{url}{self.cfg['paths']['state']}", json=body) as r:
                return await r.json(content_type=None) if r.status == 200 else {"error": r.status}

    async def apply_ir(self, device_id: str, ir: EffectIR,
                       segment: Optional[Segment] = None) -> dict[str, Any]:
        """WLED не ест IR — сначала трансляция IR->native (делает сервер),
        сюда приходит уже PortResult.target_native.args в state['native'].
        Маппинг классов — в WLED_CONFIG['ir_class_map'] (настраивается YAML)."""
        raise UnsupportedError("WLED requires IR->native translation first")

    async def subscribe(self, device_id: str,
                        cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        url = self._url(device_id).replace("http", "ws") + self.cfg["paths"]["ws"]
        reconnect = self.cfg["ws_reconnect_s"]
        async def _loop():
            if aiohttp is None:
                return
            while True:
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.ws_connect(url) as ws:
                            async for msg in ws:
                                if msg.data == "ping":
                                    await ws.send_str("{\"i\":true}")
                                else:
                                    try:
                                        await cb(json.loads(msg.data))
                                    except Exception:
                                        pass
                except Exception:
                    await asyncio.sleep(reconnect)  # ретрай офлайн-устойчивости
        asyncio.create_task(_loop())

    def _url(self, device_id: str) -> str:
        return self._nodes[device_id].endpoints[0].url
