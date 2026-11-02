"""MQTT-слой: локальный брокер (paho) + нормализация топиков экосистем
в каноническое дерево + LWT/retain для детекции офлайна.

Облачных брокеров нет (решение D): только localhost Mosquitto/paho-брокер,
заданный в конфиге. Схемы топиков каждой экосистемы маппятся сюда адаптерами.

Каноническое дерево:
  ct/v1/<device_id>/state            (retain)
  ct/v1/<device_id>/available        (LWT: online/offline, retain)
  ct/v1/<device_id>/set              (команда вниз->устройству, JSON)
  ct/v1/<device_id>/ir               (EffectIR JSON, если прошивка ест IR)
  ct/v1/<device_id>/telemetry/<kind> (звук и прочее)
  ct/v1/sync/tick                    (общий тик стендов, retain=нет)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

import paho.mqtt.client as mqtt

CANON_PREFIX = "ct/v1"


class MqttBridge:
    """Мост к локальному брокеру с async-обёрткой поверх paho-потока."""

    def __init__(self, host: str = "127.0.0.1", port: int = 1883,
                 client_id: str = "chromatransposer"):
        self.host, self.port = host, port
        # paho >= 2.x: единый CallbackAPIVersion.VERSION2
        try:
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                       client_id=client_id, protocol=mqtt.MQTTv5)
        except AttributeError:  # старый paho 1.x
            self._client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv5)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._handlers: list[tuple[str, Callable[[str, dict], Awaitable[None]]]] = []
        self._connected = asyncio.Event()

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect_async(self.host, self.port, keepalive=30)
        self._client.loop_start()
        await asyncio.wait_for(self._connected.wait(), timeout=10)

    def _on_connect(self, client, userdata, flags, rc, *a):
        if rc == 0:
            self._loop.call_soon_threadsafe(self._connected.set)
            for topic, _ in self._handlers:
                client.subscribe(topic)

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            payload = {"raw": msg.payload.decode(errors="replace")}
        for pattern, handler in self._handlers:
            if mqtt.topic_matches_sub(pattern, msg.topic):
                fut = asyncio.run_coroutine_threadsafe(
                    handler(msg.topic, payload), self._loop)
                fut.result(timeout=5)

    async def publish(self, topic: str, payload: dict, retain: bool = False,
                      qos: int = 1) -> None:
        await self._loop.run_in_executor(
            None, lambda: self._client.publish(
                topic, json.dumps(payload), qos=qos, retain=retain))

    async def publish_lwt(self, topic: str) -> None:
        """Установить LWT: при обрыве связи сервера брокер опубликует offline."""
        self._client.will_set(topic, json.dumps({"state": "offline"}),
                              qos=1, retain=True)

    def on(self, topic_pattern: str,
           handler: Callable[[str, dict], Awaitable[None]]) -> None:
        self._handlers.append((topic_pattern, handler))
        if self._client.is_connected():
            self._client.subscribe(topic_pattern)

    # --- канонические хелперы ------------------------------------------------
    @staticmethod
    def t_state(dev: str) -> str: return f"{CANON_PREFIX}/{dev}/state"
    @staticmethod
    def t_avail(dev: str) -> str: return f"{CANON_PREFIX}/{dev}/available"
    @staticmethod
    def t_set(dev: str) -> str: return f"{CANON_PREFIX}/{dev}/set"
    @staticmethod
    def t_ir(dev: str) -> str: return f"{CANON_PREFIX}/{dev}/ir"
    @staticmethod
    def t_telemetry(dev: str, kind: str) -> str:
        return f"{CANON_PREFIX}/{dev}/telemetry/{kind}"
    T_TICK = f"{CANON_PREFIX}/sync/tick"

    async def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
