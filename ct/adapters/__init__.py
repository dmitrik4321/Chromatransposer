"""Пакет южных адаптеров. Новые экосистемы — плагины: реализация
ct.adapters.base.SouthboundAdapter и регистрация в ADAPTERS."""

from ct.adapters.wled import WledAdapter
from ct.adapters.gyverhub import GyverHubAdapter
from ct.adapters.generic import GenericAdapter
from ct.adapters.native_ct import NativeCtAdapter

ADAPTERS = {
    "wled": WledAdapter,
    "gyverhub": GyverHubAdapter,
    "generic": GenericAdapter,
    "native_ct": NativeCtAdapter,
}
