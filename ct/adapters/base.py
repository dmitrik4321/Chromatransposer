"""Южный интерфейс адаптера. Плагины добавляются реализацией этого ABC.

КонTRACT: discover / capabilities / get_state / set_state / subscribe.
set_state получает либо нативную команду экосистемы, либо EffectIR
(для прошивок native_ct и WLED через эмуляцию IR-эффекта сегментами).
"""
from __future__ import annotations

import abc
from typing import Any, Awaitable, Callable, Optional

from ct.core.ir import EffectIR, Segment
from ct.core.registry import DeviceNode


class SouthboundAdapter(abc.ABC):
    """Базовый класс коннектора к одной экосистеме."""

    name: str = "abstract"
    ecosystem: str = "abstract"

    @abc.abstractmethod
    async def discover(self) -> list[DeviceNode]:
        """Найти устройства экосистемы в LAN (mDNS/сканирование/broker-scan)."""

    @abc.abstractmethod
    async def capabilities(self, device_id: str) -> DeviceNode:
        """Опросить топологию, список родных эффектов, эндпоинты, фичи."""

    @abc.abstractmethod
    async def get_state(self, device_id: str) -> dict[str, Any]:
        """Текущее состояние в канонических терминах: on, bri, effect, palette..."""

    @abc.abstractmethod
    async def set_state(self, device_id: str, state: dict[str, Any],
                        segment: Optional[Segment] = None) -> dict[str, Any]:
        """Команда устройству. Возвращает принятый reported-state.

        segment задаёт виртуальный пиксель, если устройство — часть общей ленты.
        """

    async def apply_ir(self, device_id: str, ir: EffectIR,
                       segment: Optional[Segment] = None) -> dict[str, Any]:
        """Применить IR-эффект. По умолчанию — через set_state с сериализованным IR.

        Адаптеры со supports_ir=False должны переопределить и транслировать
        IR -> native (см. ct.core.translator), либо бросать UnsupportedError.
        """
        raise UnsupportedError(f"adapter '{self.name}' cannot apply IR directly")

    @abc.abstractmethod
    async def subscribe(self, device_id: str,
                        cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Подписка на изменения состояния (WS/MQTT/poll)."""

    async def close(self) -> None:
        pass


class UnsupportedError(RuntimeError):
    """Цель не поддерживает класс эффекта — явный честный отказ."""
