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
    "mdns": {                        # автоматический поиск узлов в LAN
        "enabled": True,             # false -> только статические hosts из YAML
        "service_type": "_wled._tcp.local.",
        "timeout_s": 2.0,
    },
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
        hosts = list(self.hosts)
        for h in await self._mdns_hosts():       # дополнение к статическим host-ам
            if h not in hosts:
                hosts.append(h)
        timeout = aiohttp.ClientTimeout(self.cfg["http_timeout_s"])
        async with aiohttp.ClientSession(timeout=timeout) as s:
            for host in hosts:
                try:
                    async with s.get(f"http://{host}{self.cfg['paths']['info']}") as r:
                        info = await r.json()
                    node = self._node_from_info(host, info)
                    self._nodes[node.id] = node
                    found.append(node)
                except Exception:
                    continue
        return found

    async def _mdns_hosts(self) -> list[str]:
        """mDNS-дискавери (_wled._tcp): находим узлы, не перечисленные в YAML."""
        md = self.cfg.get("mdns", {})
        if not md.get("enabled"):
            return []
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:
            return []

        def _scan():
            zc = Zeroconf()
            hosts: list[str] = []
            try:
                browser = ServiceBrowser(zc, md["service_type"],
                                         lambda *_a: None)  # noqa: F841
                import time as _t
                _t.sleep(md.get("timeout_s", 2.0))
                for st in zc.get_service_info(md["service_type"], n) or []:
                    pass
                # проще: собрать через record cache
                from zeroconf import DNSQuestion, TYPE_A
                infos = zc.cache.all_by_name(None) if hasattr(zc.cache, "all_by_name") else {}
                del infos
                for name in zc.names():
                    if name.endswith(md["service_type"]):
                        info = zc.get_service_info(md["service_type"], name)
                        if info:
                            hosts.extend(str(ip) for ip in info.addresses)
            finally:
                zc.close()
            return hosts

        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, _scan),
                                          md.get("timeout_s", 2.0) + 2)
        except Exception:
            return []

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
