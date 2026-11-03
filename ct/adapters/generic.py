"""Generic-адаптер: HTTP/MQTT/UDP + конфиг маппинга, чтобы не писать код
под каждую «лапшу». Маппинг описывается в YAML (configs/generic/*.yaml):

  device_id: lamp-desk
  transport: http            # http | mqtt | udp
  url: http://192.168.1.77
  discovery:                 # как понять, что жив
    probe_url: /status
  state_map:                 # родные поля -> канонические
    power: on
    dim: brightness
  command_map:               # канонические -> родные (по умолчанию обратный state_map)
    on: power
  ir_endpoint: /effect       # если устройство умеет есть IR JSON напрямую
  segment_params: {offset: start, length: count}   # виртуальные пиксели, если есть
  telemetry_map: {mic.level: audio_level}
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

from ct.adapters.base import SouthboundAdapter, UnsupportedError
from ct.core.ir import EffectIR, Segment, Topology
from ct.core.registry import DeviceNode, Endpoint, Ecosystem, TransportKind

try:
    import aiohttp
except ImportError:
    aiohttp = None

# ------------------------------ КОНФИГ АДАПТЕРА ------------------------------
# Всё меняемое — здесь; переопределяется секцией adapters[].config в YAML.
GENERIC_CONFIG = {
    "probe_timeout_s": 2.0,     # таймаут discovery-пробы «жив ли»
    "poll_interval_s": 5.0,     # опрос состояния, если подписки нет
}


class GenericAdapter(SouthboundAdapter):
    name = "generic"
    ecosystem = "generic"

    def __init__(self, configs: list[dict], bridge=None, config: Optional[dict] = None):
        self.cfg = {**GENERIC_CONFIG, **(config or {})}   # YAML поверх дефолтов
        self.cfgs = {c["device_id"]: c for c in configs}
        self.bridge = bridge
        self._nodes: dict[str, DeviceNode] = {}

    async def discover(self) -> list[DeviceNode]:
        found = []
        for dev_id, cfg in self.cfgs.items():
            node = DeviceNode(
                id=dev_id, name=cfg.get("name", dev_id),
                ecosystem=Ecosystem.GENERIC, adapter=self.name,
                topology=Topology(**cfg.get("topology", {"kind": "strip", "length": 0})),
                supports_ir=bool(cfg.get("ir_endpoint")),
                supports_segments=bool(cfg.get("segment_params")),
                endpoints=[Endpoint(kind=TransportKind(cfg.get("transport", "http")),
                                    url=cfg.get("url"),
                                    base_topic=cfg.get("base_topic"),
                                    port=cfg.get("port"))],
            )
            if await self._alive(dev_id):
                node.mark_seen()
            self._nodes[dev_id] = node
            found.append(node)
        return found

    async def capabilities(self, device_id: str) -> DeviceNode:
        cfg = self.cfgs[device_id]
        node = self._nodes[device_id]
        probe = cfg.get("discovery", {}).get("probe_url")
        if probe and aiohttp and cfg.get("transport") == "http":
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(cfg["url"].rstrip("/") + probe) as r:
                        info = await r.json()
                node.topology.length = int(info.get("leds", node.topology.length))
            except Exception:
                pass
        return node

    async def _alive(self, device_id: str) -> bool:
        cfg = self.cfgs[device_id]
        if cfg.get("transport") == "http" and aiohttp:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(cfg["url"] + cfg.get("discovery", {}).get("probe_url", "/"),
                                     timeout=aiohttp.ClientTimeout(
                                         self.cfg["probe_timeout_s"])):
                        return True
            except Exception:
                return False
        return True

    def _to_native(self, device_id: str, canon: dict) -> dict:
        cm = self.cfgs[device_id].get("command_map", {})
        return {cm.get(k, k): v for k, v in canon.items()}

    def _to_canon(self, device_id: str, raw: dict) -> dict:
        sm = self.cfgs[device_id].get("state_map", {})
        return {sm.get(k, k): v for k, v in raw.items()}

    async def get_state(self, device_id: str) -> dict[str, Any]:
        cfg = self.cfgs[device_id]
        if cfg.get("transport") == "mqtt" and self.bridge:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()

            async def _h(t, p):
                if not fut.done():
                    fut.set_result(p)
            self.bridge.on(f"{cfg['base_topic']}/state", _h)
            try:
                raw = await asyncio.wait_for(fut, 3)
            except asyncio.TimeoutError:
                raw = {}
            return self._to_canon(device_id, raw)
        if aiohttp:
            async with aiohttp.ClientSession() as s:
                async with s.get(cfg["url"] + cfg.get("state_url", "/status")) as r:
                    return self._to_canon(device_id, await r.json())
        return {}

    async def set_state(self, device_id: str, state: dict[str, Any],
                        segment: Optional[Segment] = None) -> dict[str, Any]:
        cfg = self.cfgs[device_id]
        native = self._to_native(device_id, state)
        if segment is not None and "segment_params" in cfg:
            sp = cfg["segment_params"]
            native[sp.get("offset", "offset")] = segment.start
            native[sp.get("length", "length")] = segment.length
        t = cfg.get("transport")
        if t == "mqtt" and self.bridge:
            await self.bridge.publish(f"{cfg['base_topic']}/set", native, retain=True)
        elif t == "udp":
            loop = asyncio.get_running_loop()
            sock = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, remote_addr=(cfg["host"], cfg["port"]))
            sock[0].sendto(json.dumps(native).encode())
            sock[1].close()
        elif t == "http" and aiohttp:
            async with aiohttp.ClientSession() as s:
                await s.post(cfg["url"] + cfg.get("command_url", "/set"), json=native)
        return native

    async def apply_ir(self, device_id: str, ir: EffectIR,
                       segment: Optional[Segment] = None) -> dict[str, Any]:
        cfg = self.cfgs[device_id]
        if not cfg.get("ir_endpoint"):
            raise UnsupportedError(f"generic device {device_id} has no ir_endpoint")
        payload = json.loads(ir.model_dump_json())
        if segment:
            payload["segment"] = segment.model_dump()
        if cfg.get("transport") == "mqtt" and self.bridge:
            await self.bridge.publish(f"{cfg['base_topic']}/ir", payload)
        elif aiohttp:
            async with aiohttp.ClientSession() as s:
                await s.post(cfg["url"] + cfg["ir_endpoint"], json=payload)
        return {"sent": cfg["ir_endpoint"]}

    async def subscribe(self, device_id: str,
                        cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        cfg = self.cfgs[device_id]
        if cfg.get("transport") == "mqtt" and self.bridge:
            async def _h(t, p):
                await cb(self._to_canon(device_id, p))
            self.bridge.on(f"{cfg['base_topic']}/+", _h)
        else:  # poll fallback — офлайн-устойчивость через ретраи
            async def _poll():
                while True:
                    try:
                        await cb(await self.get_state(device_id))
                    except Exception:
                        pass
                    await asyncio.sleep(cfg.get("poll_interval",
                                                self.cfg["poll_interval_s"]))
            asyncio.create_task(_poll())
