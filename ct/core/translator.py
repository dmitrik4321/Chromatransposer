"""Транслятор эффектов: native(source) -> IR -> native(target).

Внутри: нормализация диапазонов параметров (WLED speed 0..255, Gyver свои
шкалы), конвертер палитр (форматы градиентов различаются), обработка потерь:
нет класса у цели -> деградация в композицию примитивов или честный
unsupported. Топологически-привязанные эффекты портируются только с
декларированными потерями.
"""
from __future__ import annotations

from typing import Optional

from ct.core.ir import (
    ColorStop, EffectClass, EffectIR, EffectParams, NativeRef,
    Palette, PortResult, Topology,
)
from ct.core.registry import DeviceNode

# ------------------------------ КОНФИГ ТРАНСЛЯТОРА ----------------------------
# Всё меняемое собрано здесь; сервер переопределяет любой ключ секцией
# `translator:` в configs/server.yaml (см. configure()). Формат YAML-переопределения:
#   translator:
#     fallback_order: [static, breathing]      # деградационная лестница
#     matrix_only_default: [fire, plasma]      # классы, привязанные к матрице
#     topology_exempt: {gyverlamp2: [fire]}    # исключения по факту прошивок
#     ecosystem_maps: {wled: {myfx: perlin}}   # пополнение карт (небольшая магия)
TRANSLATOR_CONFIG = {
    "fallback_order": [EffectClass.STATIC, EffectClass.BREATHING,
                       EffectClass.COLORSWOKE],
    "matrix_only_default": {EffectClass.FIRE, EffectClass.PLASMA, EffectClass.RAIN},
    "topology_exempt": {"gyverlamp2": {EffectClass.FIRE, EffectClass.COLORSWOKE,
                                       EffectClass.RAINBOW}},
    "gyverlamp2_scales": {"speed": (0, 255), "scale": (0, 255),
                          "brightness": (0, 255)},
}


def configure(cfg: dict) -> None:
    """Переопределение TRANSLATOR_CONFIG из YAML (вызывается Server.start)."""
    for k, v in (cfg or {}).items():
        if k == "fallback_order":
            TRANSLATOR_CONFIG[k] = [EffectClass(x) for x in v]
        elif k in ("matrix_only_default",):
            TRANSLATOR_CONFIG[k] = {EffectClass(x) for x in v}
        elif k == "topology_exempt":
            TRANSLATOR_CONFIG[k] = {eco: {EffectClass(x) for x in s}
                                    for eco, s in v.items()}
        elif k == "ecosystem_maps":
            for eco, m in v.items():
                ECOSYSTEM_MAPS.setdefault(eco, {}).update(
                    {name.lower(): EffectClass(cls) for name, cls in m.items()})
        else:
            TRANSLATOR_CONFIG[k] = v

# --- Словари соответствий родных имён эффектов -> класс IR ------------------
# Пополняется по мере роста адаптеров. Ключ — экосистема, значение —
# {имя_родного_эффекта(нижний регистр): класс}.

WLED_MAP: dict[str, EffectClass] = {
    "perlinnoise": EffectClass.PERLIN,
    "perlin": EffectClass.PERLIN,
    "plasma": EffectClass.PLASMA,
    "fire2012": EffectClass.FIRE,
    "fireworks": EffectClass.RAIN,
    "rain": EffectClass.RAIN,
    "colorsweep": EffectClass.COLORSWOKE,
    "staticgrad": EffectClass.STATIC,
    "solid": EffectClass.STATIC,
    "blink": EffectClass.BLINK,
    "twinklecat": EffectClass.TWINKLE,
    "twinkle": EffectClass.TWINKLE,
    "breathe": EffectClass.BREATHING,
    "breathing": EffectClass.BREATHING,
    "rainbow": EffectClass.RAINBOW,
    "gradient": EffectClass.COLORSWOKE,
}

# Стоковая прошивка AlexGyver/GyverLamp2 (данные сообщества: smeleshkin/GyverLamp2HA):
# ровно 10 эффектов — perlin, color, cycle, gradient, particles, fire, fire2020,
# confetti, tornado, clock. Остальное — честный unsupported или деградация.
GYVERLAMP2_MAP: dict[str, EffectClass] = {
    "perlin": EffectClass.PERLIN,
    "color": EffectClass.STATIC,
    "cycle": EffectClass.COLORSWOKE,
    "gradient": EffectClass.COLORSWOKE,
    "particles": EffectClass.TWINKLE,     # приблизительное соответствие: мерцание частиц (декларируется)
    "fire": EffectClass.FIRE,
    "fire2020": EffectClass.FIRE,
    "confetti": EffectClass.TWINKLE,      # приблизительное соответствие
    "tornado": EffectClass.CUSTOM,        # матричный вихрь — топология-привязанный
    "clock": EffectClass.CUSTOM,          # дисплей-эффект, не переносится 1:1
}

# GyverHub — конструктор UI: набор эффектов зависит от скетча пользователя,
# поэтому карта ниже лишь типовые имена из примеров Gyver (уточняется по YAML).
GYVER_MAP: dict[str, EffectClass] = {
    "noise": EffectClass.PERLIN,
    "perlin": EffectClass.PERLIN,
    "plasma": EffectClass.PLASMA,
    "fire": EffectClass.FIRE,
    "rain": EffectClass.RAIN,
    "matrixrain": EffectClass.RAIN,
    "candle": EffectClass.FIRE,
    "blink": EffectClass.BLINK,
    "twinkle": EffectClass.TWINKLE,
    "breath": EffectClass.BREATHING,
    "rainbow": EffectClass.RAINBOW,
    "fade": EffectClass.BREATHING,
    "solid": EffectClass.STATIC,
}

NATIVE_CT_MAP: dict[str, EffectClass] = {c.value: c for c in EffectClass}

ECOSYSTEM_MAPS: dict[str, dict[str, EffectClass]] = {
    "wled": WLED_MAP,
    "gyverhub": GYVER_MAP,
    "gyverlamp2": GYVERLAMP2_MAP,
    "native_ct": NATIVE_CT_MAP,
}

# Классы, требующие матричной топологии, ПО ФАКТУ конкретных прошивок
# (сверено с GyverLamp2HA/smeleshkin: fire/fire2020/particles/confetti и cycle
#  стоковая лампа рисует на 1D-кольце; а вот tornado/clock — матричные).
# Наборы живут в TRANSLATOR_CONFIG и переопределяются из YAML (см. configure()).
CUSTOM_MATRIX_ONLY = {EffectClass.CUSTOM}   # tornado/clock: custom + матрица


def _matrix_only(ir_class: EffectClass, ecosystem: str) -> bool:
    exempt = TRANSLATOR_CONFIG["topology_exempt"].get(ecosystem, set())
    default_matrix = TRANSLATOR_CONFIG["matrix_only_default"]
    if ecosystem == "gyverlamp2":
        if ir_class is EffectClass.CUSTOM:
            return True                   # tornado/clock — только матрица, 1:1 нет
        return ir_class in (default_matrix - exempt)
    return ir_class in default_matrix


def _norm(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 0.5
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _denorm(n: float, lo: float, hi: float) -> int:
    return int(round(lo + max(0.0, min(1.0, n)) * (hi - lo)))


# --- native -> IR -----------------------------------------------------------

def native_to_ir(ref: NativeRef, args: Optional[dict] = None) -> EffectIR:
    """Отображение родного эффекта экосистемы в канонический IR."""
    a = args or ref.args
    cls = ECOSYSTEM_MAPS.get(ref.ecosystem, {}).get(ref.effect.lower(), EffectClass.CUSTOM)
    losses: list[str] = []

    params = EffectParams()
    if ref.ecosystem == "wled":
        # WLED: speed/intensity/size 0..255, brightness 0..255
        params.speed = _norm(float(a.get("sx", a.get("speed", 128))), 0, 255)
        params.density = _norm(float(a.get("ix", a.get("intensity", 128))), 0, 255)
        params.scale = _norm(float(a.get("fx", a.get("size", 128))), 0, 255)
        params.brightness = _norm(float(a.get("bri", a.get("brightness", 255))), 0, 255)
    elif ref.ecosystem == "gyverhub":
        # GyverHub-скетч: шкалы задаёт автор прошивки; дефолт — проценты 0..100
        params.speed = _norm(float(a.get("speed", 50)), 0, 100)
        params.density = _norm(float(a.get("density", a.get("amount", 50))), 0, 100)
        params.scale = _norm(float(a.get("scale", 50)), 0, 100)
        params.brightness = _norm(float(a.get("bright", a.get("brightness", 100))), 0, 100)
    elif ref.ecosystem == "gyverlamp2":
        # GyverLamp2: speed/scale/bright — байты 0..255 (проверено по протоколу GL);
        # шкалы настраиваются в TRANSLATOR_CONFIG["gyverlamp2_scales"] (YAML).
        sc = TRANSLATOR_CONFIG["gyverlamp2_scales"]
        params.speed = _norm(float(a.get("speed", 128)), *sc["speed"])
        params.scale = _norm(float(a.get("scale", 128)), *sc["scale"])
        params.brightness = _norm(float(a.get("bright", 255)), *sc["brightness"])
        if int(a.get("sound_react", 1)) != 1:
            losses.append("soundReact выключен — реакция на звук теряется при чтении")
    else:
        for k in ("speed", "scale", "density", "blur", "brightness"):
            if k in a:
                setattr(params, k, float(a[k]))

    palette = _palette_from_native(ref.ecosystem, a)
    if cls is EffectClass.CUSTOM:
        losses.append(f"unknown native effect '{ref.effect}' kept as opaque custom")

    return EffectIR(**{"class": cls}, params=params, palette=palette,
                    losses=losses, origin={"ecosystem": ref.ecosystem, "effect": ref.effect})


def _palette_from_native(ecosystem: str, a: dict) -> Palette:
    stops: list[ColorStop] = []
    if ecosystem == "wled":
        # палитры WLED: массив "#rrggbb" либо primary/secondary/accent
        pal = a.get("palette")
        if isinstance(pal, list):
            n = max(1, len(pal) - 1)
            stops = [ColorStop(pos=i / n, color=c) for i, c in enumerate(pal)]
        elif all(k in a for k in ("col1", "col2")):
            stops = [ColorStop(pos=0.0, color=a["col1"]), ColorStop(pos=1.0, color=a["col2"])]
    elif ecosystem == "gyverhub":
        pal = a.get("pal", a.get("palette"))
        if isinstance(pal, list):
            n = max(1, len(pal) - 1)
            stops = [ColorStop(pos=i / n, color=c) for i, c in enumerate(pal)]
    elif ecosystem == "gyverlamp2":
        # палитра прошивки — встроенный id 2..32, стопы неизвестны наружу:
        # сохраняем имя; конвертер обязан честно декларировать потерю градиента
        from ct.adapters.gyverlamp2_protocol import palette_name, PALETTE_FIRST_ID
        pid = a.get("palette", a.get("pal"))
        if isinstance(pid, int):
            return Palette(name=palette_name(pid), stops=[])
        if isinstance(pid, str):
            return Palette(name=pid, stops=[])
    return Palette(stops=stops)


# --- IR -> native -----------------------------------------------------------

def ir_to_native(ir: EffectIR, target: DeviceNode) -> PortResult:
    """IR -> нативная команда цели. Явная деградация/отказ вместо подмены."""
    notes: list[str] = []
    tgt_map = ECOSYSTEM_MAPS.get(target.ecosystem, {})
    reverse = {v: k for k, v in tgt_map.items()}

    # 1. Прошивка-интерпретатор ест IR напрямую.
    if target.supports_ir:
        return PortResult(ir=ir,
                          target_native=NativeRef(ecosystem=target.ecosystem.value,
                                                  effect=ir.class_.value,
                                                  args=ir.params.model_dump()),
                          notes=["direct IR"])

    # 2. Топологические потери: матричный эффект на ленту и наоборот.
    if _matrix_only(ir.class_, target.ecosystem.value) and not target.topology.is_matrix:
        notes.append(f"'{ir.class_.value}' is topology-bound; degraded for strip")
        return _degrade(ir, target, reverse, notes)
    if target.topology.is_matrix and ir.class_ is EffectClass.RAIN:
        notes.append("rain on matrix: 1D projection, spatial losses declared")

    # 3. Прямое соответствие классу.
    # кандидаты: нативные имена цели, объявленные в её capabilities
    candidates = [k for k, v in tgt_map.items()
                  if v is ir.class_ and (not target.native_effects
                                         or k in target.effect_names())]
    native_name = candidates[0] if candidates else None
    if native_name:
        args = _encode_params(ir.params, target.ecosystem.value)
        pal_enc = _encode_palette(ir.palette, target.ecosystem.value)
        if "_loss" in pal_enc:               # не_builtin палитра -> декларация, а не ключ в args
            notes.append(pal_enc.pop("_loss"))
        args.update(pal_enc)
        return PortResult(ir=ir,
                          target_native=NativeRef(ecosystem=target.ecosystem.value,
                                                  effect=native_name, args=args),
                          notes=notes)

    # 4. Деградация в композицию примитивов либо честный unsupported.
    return _degrade(ir, target, reverse, notes)


def _degrade(ir: EffectIR, target: DeviceNode, reverse: dict, notes: list[str]) -> PortResult:
    available = set(reverse.keys())
    for fb in TRANSLATOR_CONFIG["fallback_order"]:   # лестница настраивается YAML
        if fb in available and fb is not ir.class_:
            deg_losses = [f"'{ir.class_.value}' -> '{fb.value}' composition"] + list(notes)
            deg = EffectIR(**{"class": EffectClass.COMPOSITION},
                           params=ir.params, palette=ir.palette,
                           losses=deg_losses, origin=ir.origin)
            args = _encode_params(ir.params, target.ecosystem.value)
            pal_enc = _encode_palette(ir.palette, target.ecosystem.value)
            if "_loss" in pal_enc:
                notes.append(pal_enc.pop("_loss"))
            args.update(pal_enc)
            notes.append(f"degraded: {ir.class_.value} -> {fb.value}")
            return PortResult(ir=ir, degraded_to=deg,
                              target_native=NativeRef(ecosystem=target.ecosystem.value,
                                                      effect=reverse[fb], args=args),
                              notes=notes)
    notes.append(f"target '{target.id}' has no compatible class for '{ir.class_.value}'")
    return PortResult(ir=ir, unsupported=True, notes=notes)


def _encode_params(params: EffectParams, ecosystem: str) -> dict:
    if ecosystem == "wled":
        return {
            "sx": _denorm(params.speed, 0, 255),
            "ix": _denorm(params.density, 0, 255),
            "fx": _denorm(params.scale, 0, 255),
            "bri": _denorm(params.brightness, 0, 255),
        }
    if ecosystem == "gyverhub":
        return {
            "speed": _denorm(params.speed, 0, 100),
            "density": _denorm(params.density, 0, 100),
            "scale": _denorm(params.scale, 0, 100),
            "bright": _denorm(params.brightness, 0, 100),
        }
    if ecosystem == "gyverlamp2":
        # режимы GL,2 — байтовые поля 0..255; density у лампы нет (потеря
        # декларируется вызывающим через notes), hue кодируется отдельно
        return {
            "speed": _denorm(params.speed, 0, 255),
            "scale": _denorm(params.scale, 0, 255),
            "bright": max(1, _denorm(params.brightness, 0, 255)),
        }
    return params.model_dump(exclude={"extra"})


def _encode_palette(palette: Palette, ecosystem: str) -> dict:
    colors = [s.color for s in palette.stops]
    if ecosystem == "gyverlamp2":
        # прошивка принимает только id встроенной палитры 2..32 (стопы не шлются)
        from ct.adapters.gyverlamp2_protocol import palette_id
        try:
            return {"palette": palette_id(palette.name or "rainbow")}
        except ValueError:
            return {"palette": 12, "_loss": f"palette '{palette.name}' not builtin; rainbow used"}
    if not colors:
        return {}
    if ecosystem == "wled":
        out = {"col1": colors[0]}
        if len(colors) > 1:
            out["col2"] = colors[-1]
        if len(colors) > 2:
            out["col3"] = colors[len(colors) // 2]
        return out
    return {"pal": colors}


def port(source_ref: NativeRef, target: DeviceNode,
         source_args: Optional[dict] = None) -> PortResult:
    """Полный цикл: native(source) -> IR -> native(target)."""
    ir = native_to_ir(source_ref, source_args)
    return ir_to_native(ir, target)
