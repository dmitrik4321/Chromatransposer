"""Плоское состояние (k=v) -> Effect IR: предпросмотр для простых команд.

Без этой прослойки ASCII-логгер видел бы desired-IR только когда клиент
явно прислал JSON эффекта (`ir ...` / port). Команды вида `set dev fx=perlin
sx=200 col1=#ff0000 bri=128` остались бы «нет desired IR — показываю
выключенным», что обманывало бы картинку. Здесь состояние экосистемы
разбирается через тот же translator, что и портирование: NativeRef -> IR
(нормализация шкал WLED/Gyver в 0..1, палитра из col1/col2 или массива).

Граница сохраняется: это ПРЕДПОЛОЖЕНИЕ control plane по тому же IR, который
уходит на юг; реальную ленту считает прошивка.
"""
from __future__ import annotations

from typing import Optional

from ct.core.ir import EffectClass, EffectIR, NativeRef, Palette, ColorStop
from ct.core.registry import Ecosystem
from ct.core.translator import ECOSYSTEM_MAPS, native_to_ir

# ------------------------------ КОНФИГ ----------------------------------------
# Всё меняемое — здесь; переопределяется секцией `state_ir:` configs/server.yaml.
STATE_IR_CONFIG = {
    # ключи плоского состояния, которые считаются «эффектом» (по приоритету)
    "effect_keys": ["fx", "effect", "mode"],
    # ключи аргументов, уходящие в params/palette через native_to_ir
    "passthrough_keys": ["sx", "ix", "fx_size", "speed", "intensity", "size",
                         "scale", "density", "blur", "bri", "brightness",
                         "col1", "col2", "col3", "palette", "pal"],
    # если эффект не назван, но задан цвет — считаем статикой этого цвета
    "color_keys": ["col1", "color"],
}


def configure(cfg: dict) -> None:
    for k, v in (cfg or {}).items():
        STATE_IR_CONFIG[k] = v


def _norm_color(c) -> Optional[str]:
    """WLED-форматы цвета -> '#rrggbb'. Принимает строку или int 0xRRGGBB."""
    if isinstance(c, str):
        s = c.strip()
        if s.startswith("#") and len(s) == 7:
            return s.lower()
        if len(s) == 6:
            try:
                int(s, 16)
                return "#" + s.lower()
            except ValueError:
                return None
        return None
    if isinstance(c, (int, float)):
        v = int(c) & 0xFFFFFF
        return f"#{v:06x}"
    return None


def state_to_ir(state: dict, ecosystem: str) -> Optional[EffectIR]:
    """Плоское состояние -> IR, либо None если выводить нечего.

    Честно: неизвестный родной эффект станет CUSTOM с потерей opaque — ровно
    как при портировании; мы не рисуем красивую ложь вместо правды.
    """
    if not state:
        return None
    args: dict = {}
    effect_name: Optional[str] = None
    for key in STATE_IR_CONFIG["effect_keys"]:
        if key in state:
            effect_name = str(state[key])
            break
    for key in STATE_IR_CONFIG["passthrough_keys"]:
        if key in state:
            args[key] = state[key]

    if effect_name is None:
        # эффекта нет: есть ли цвет? тогда static по палитре из цветов
        cols = [_norm_color(state.get(k)) for k in STATE_IR_CONFIG["color_keys"]]
        cols = [c for c in cols if c]
        if not cols and isinstance(state.get("palette"), list):
            cols = [c for c in (_norm_color(x) for x in state["palette"]) if c]
        if not cols:
            return None  # on/bri без цвета и эффекта — рендеру нечего показать
        stops = ([ColorStop(pos=i / max(1, len(cols) - 1), color=c)
                  for i, c in enumerate(cols)] if len(cols) > 1
                 else [ColorStop(pos=0.0, color=cols[0])])
        ir = EffectIR(**{"class": EffectClass.STATIC},
                      palette=Palette(name="from_state", stops=stops),
                      losses=[] if len(cols) == 1 else
                      ["несколько цветов в static — показан градиент стопов"])
    else:
        ref = NativeRef(ecosystem=ecosystem, effect=effect_name, args=args)
        ir = native_to_ir(ref)
        # цвета поверх разобранного эффекта (native_to_ir их уже видит в args,
        # но col* могли прийти как int — нормализуем стопы постфактум)
        stops = []
        for k in ("col1", "col2", "col3"):
            c = _norm_color(args.get(k))
            if c:
                stops.append(c)
        if stops and not ir.palette.stops:
            n = max(1, len(stops) - 1)
            ir.palette = Palette(name="from_state",
                                 stops=[ColorStop(pos=i / n, color=c)
                                        for i, c in enumerate(stops)])

    # яркость из flat-состояния важнее того, что натолковал translator
    for bk in ("bri", "brightness"):
        if bk in state:
            raw = float(state[bk])
            # 0..255 у wled/gyverlamp2/native_ct; 0..100 у gyverhub-дефолта
            hi = 100.0 if ecosystem == Ecosystem.GYVERHUB.value else 255.0
            if raw <= 1.0:
                ir.params.brightness = max(0.0, min(1.0, raw))
            else:
                ir.params.brightness = max(0.0, min(1.0, raw / hi))
            break
    if state.get("on") in (False, 0, "false", "off"):
        ir.params.brightness = 0.0
    return ir
