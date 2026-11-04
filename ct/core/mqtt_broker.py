"""Встроенный локальный MQTT-брокер (решение D + «прослойка сама поднимает MQTT»).

AMQTT выбран как asyncio-нативный брокер на Python: не требует ставить
Mosquitto отдельным демоном — сервер Chromatransposer поднимает брокер сам
в том же процессе. Только localhost/LAN, без облака.

ВАЖНО (проверено на amqtt 0.12):
  * брокер принимает MQTT 3.1.1; MQTTv5 CONNECT отклоняется ("Unsupported
    protocol version") — поэтому MqttBridge подключается протоколом 311;
  * имя секции листенера должно быть "default" (иначе KeyError);
  * плагин авторизации задаётся в auth.plugins=["auth_anonymous"] — иначе
    идёт загрузка через EntryPoint с DeprecationWarning.

Всё меняемое — в BROKER_CONFIG ниже и в YAML (секция mqtt.broker).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

# ------------------------------ КОНФИГ БРОКЕРА -------------------------------
# Переопределяется секцией mqtt.broker в configs/server.yaml.
BROKER_CONFIG = {
    "enabled": True,              # false -> внешний брокер (host/port выше)
    "bind": "0.0.0.0:1883",       # 0.0.0.0 — чтобы ESP-устройства видели брокер в LAN
    "sys_interval_s": 0,          # 0 — не спамить $SYS-топики
    "max_inflight_messages": 20,
    "message_ttl_s": 3600,        # retain-сообщения живут час (для LWT/состояний)
}
# -----------------------------------------------------------------------------


class EmbeddedBroker:
    """Обёртка над AMQTT Broker: старт/стоп внутри event loop сервера."""

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = {**BROKER_CONFIG, **(cfg or {})}
        self._broker = None
        self._started = False

    @staticmethod
    def _quiet_amqtt_logs() -> None:
        """AMQTT очень разговорчив по умолчанию; оставляем только ошибки."""
        for name in ("amqtt", "amqtt.mqtt.protocol.handler",
                     "amqtt.broker.plugins.event_logger_plugin"):
            logging.getLogger(name).setLevel(logging.ERROR)

    async def start(self) -> None:
        if not self.cfg.get("enabled", True):
            return
        try:
            from amqtt.broker import Broker
        except ImportError as e:                      # мягкая деградация
            raise RuntimeError(
                "встроенный брокер требует 'pip install amqtt' "
                "(или включи mqtt.broker.enabled=false и запусти свой)") from e
        self._quiet_amqtt_logs()
        config = {
            "listeners": {"default": {                       # имя обязательное
                "type": "tcp",
                "bind": self.cfg["bind"],
                # ВНИМАНИЕ (проверено на amqtt 0.12): max_inflight НЕ является
                # полем листенера — dacite падает с UnexpectedDataError.
                # Задаём его только если секция явно разрешена конфигом.
                **({"max_inflight_messages": self.cfg["max_inflight_messages"]}
                   if self.cfg.get("_listener_max_inflight") else {}),
            }},
            "sys_interval": self.cfg["sys_interval_s"],
            "auth": {
                "allow-anonymous": True,   # устройства без токенов (решение E);
                                           # сегмент сети — граница доверия
                "plugins": ["auth_anonymous"],
            },
        }
        self._broker = Broker(config)
        await self._broker.start()
        self._started = True

    def bound_bind(self) -> str:
        """Фактический bind-адрес ('host:port'). Если в конфиге порт 0 (эфемерный),
        возвращает порт, реально выданный ОС — чтобы клиент моста подключался туда же.
        """
        if not self._started or not getattr(self._broker, "_servers", None):
            return self.cfg["bind"]
        try:
            srv = next(iter(self._broker._servers.values()))
            sock = srv.instance.sockets[0]
            host, port = sock.getsockname()[:2]
            return f"{host}:{port}"
        except (AttributeError, IndexError, OSError):
            return self.cfg["bind"]

    async def stop(self) -> None:
        if self._broker is not None and self._started:
            try:
                await asyncio.wait_for(self._broker.shutdown(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass          # при остановке сервера дожимать нечего
            self._started = False

    @property
    def running(self) -> bool:
        return self._started
