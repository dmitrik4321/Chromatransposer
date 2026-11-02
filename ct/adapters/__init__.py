"""Пакет южных адаптеров. Новые экосистемы — плагины: реализация
ct.adapters.base.SouthboundAdapter и регистрация в ADAPTERS."""

from ct.adapters.wled import WledAdapter
from ct.adapters.gyverhub import GyverHubAdapter
from ct.adapters.gyverlamp2 import GyverLamp2Adapter
from ct.adapters.generic import GenericAdapter
from ct.adapters.native_ct import NativeCtAdapter

ADAPTERS = {
    "wled": WledAdapter,
    "gyverhub": GyverHubAdapter,      # конструктор UI: MQTT-виджеты (GyverMatrix и пр.)
    "gyverlamp2": GyverLamp2Adapter,  # стоковая GyverLamp2: UDP «GL» (smeleshkin/GyverLamp2HA)
    "generic": GenericAdapter,
    "native_ct": NativeCtAdapter,
}
