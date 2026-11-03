"""Регресс: карты эффектов адаптеров не должны зтирать статические ECOSYSTEM_MAPS.

Сценарии:
1) configure({... "ecosystem_maps": ...}) из YAML должен ДОПОЛНЯТЬ карту
   экосистемы (update), а не заменять её — иначе уже известные слова
   деградируют до 'custom'.
2) Два коннектора к одной экосистеме (opera CDP + opera_mqtt): имена
   объединяются; пересечения перезаписываются последним словом конфига —
   это ожидаемое поведение (конфиг адаптера ближе к железу, чем статика).
3) Классификация родного эффекта идёт по merged-карте и регистронезависима.
"""
from __future__ import annotations

from ct.core.ir import NativeRef
from ct.core.translator import (
    ECOSYSTEM_MAPS,
    EffectClass,
    configure,
    native_to_ir,
)


def _snapshot(eco: str) -> dict[str, EffectClass]:
    return dict(ECOSYSTEM_MAPS.get(eco, {}))


def test_configure_updates_not_replaces():
    base = _snapshot("wled")
    assert base, "базовая WLED_MAP должна быть заполнена из модуля"
    try:
        configure({"ecosystem_maps": {"wled": {"mycandle": "fire"}}})
        merged = _snapshot("wled")
        for k, v in base.items():          # старое слово осталось
            assert merged.get(k) == v, f"потерян класс для '{k}'"
        assert merged["mycandle"] is EffectClass.FIRE  # новое добавилось
    finally:
        ECOSYSTEM_MAPS["wled"].pop("mycandle", None)   # откат для других тестов


def test_opera_two_connectors_merge_names():
    before = _snapshot("opera")
    mqtt_map = {e["effect"]: e["class"] for e in [
        {"effect": "static", "class": "static"},
        {"effect": "blink", "class": "blink"},
        {"effect": "colorsweep", "class": "colorsweep"},
        {"effect": "breathing", "class": "breathing"},
        {"effect": "mute", "class": "custom"},
        {"effect": "panic_off", "class": "custom"},
    ]}
    try:
        configure({"ecosystem_maps": {"opera": mqtt_map}})
        after = _snapshot("opera")
        for k in before:
            assert k in after, f"имя '{k}' пропало после merge"
        assert after["panic_off"] is EffectClass.CUSTOM
        assert after["blink"] is EffectClass.BLINK
    finally:
        ECOSYSTEM_MAPS["opera"] = before


def test_classification_uses_merged_map_case_insensitive():
    ir = native_to_ir(NativeRef(ecosystem="opera", effect="BLINK"))
    assert ir.class_ is EffectClass.BLINK
    ir2 = native_to_ir(NativeRef(ecosystem="opera", effect="нет-в-карте"))
    assert ir2.class_ is EffectClass.CUSTOM  # честный custom, не выдумка
