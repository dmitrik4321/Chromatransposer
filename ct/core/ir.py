"""Effect IR — каноническое параметрическое представление эффекта.

Ядро портирования: native(source) -> IR -> native(target).
IR намеренно мал: класс эффекта + нормализованные параметры + палитра
из упорядоченных цветовых стопов. Всё, что не выражается этой схемой,
при портировании деградирует явно (см. ct.core.translator).
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class EffectClass(str, Enum):
    """Канонические классы эффектов. Расширяется, не ломая схему."""

    PERLIN = "perlin"        # мягкий шумовой градиент
    PLASMA = "plasma"        # интерференция синусоид
    FIRE = "fire"            # огонь (1D/2D)
    RAIN = "rain"            # дождь/капли по ленте
    COLORSWOKE = "colorsweep"  # бегущая волна цвета
    STATIC = "static"        # статический цвет
    BLINK = "blink"          # мигание
    TWINKLE = "twinkle"      # мерцание случайных пикселей
    BREATHING = "breathing"  # плавное дыхание яркостью
    RAINBOW = "rainbow"      # радуга по топологии
    COMPOSITION = "composition"  # деградационная композиция примитивов
    CUSTOM = "custom"        # нераспознанный родной эффект, opaque payload


class ColorStop(BaseModel):
    pos: float = Field(ge=0.0, le=1.0)   # позиция в градиенте, нормализовано
    color: str                            # "#rrggbb"


class Palette(BaseModel):
    """Упорядоченные цветовые стопы. Порядок значим."""

    name: str = "default"
    stops: list[ColorStop] = Field(default_factory=list)


class Topology(BaseModel):
    """Топология узла/виртуального холста. 1D: length; 2D: width*height."""

    kind: str = "strip"          # strip | matrix
    length: int = 0              # всего пикселей (для матрицы w*h)
    width: int = 1
    height: int = 1
    color_order: str = "GRB"     # GRB | RGB | BRG ...
    gamma: float = 2.2
    max_fps: int = 60

    @property
    def is_matrix(self) -> bool:
        return self.kind == "matrix" and self.width > 1 and self.height > 1


class EffectParams(BaseModel):
    """Нормализованные параметры: все шкалы приведены к 0..1.

    speed   — общая скорость анимации
    scale   — пространственный масштаб узора
    density — плотность событий (капли, искры, звёзды)
    blur    — размытие / сглаживание
    brightness — глобальная яркость 0..1
    """

    speed: float = Field(0.5, ge=0.0, le=1.0)
    scale: float = Field(0.5, ge=0.0, le=1.0)
    density: float = Field(0.5, ge=0.0, le=1.0)
    blur: float = Field(0.0, ge=0.0, le=1.0)
    brightness: float = Field(1.0, ge=0.0, le=1.0)
    extra: dict = Field(default_factory=dict)  # экзотика без гарантий переноса


class EffectIR(BaseModel):
    """Канонический эффект. Именно этот JSON уходит в прошивку-интерпретатор."""

    class_: EffectClass = Field(alias="class")
    params: EffectParams = Field(default_factory=EffectParams)
    palette: Palette = Field(default_factory=Palette)
    # Потери при трансляции — декларируются, а не прячутся.
    losses: list[str] = Field(default_factory=list)
    origin: Optional[dict] = None  # откуда пришёл: {"ecosystem":"wled","effect":"PerlinNoise"}

    model_config = {"populate_by_name": True}


class Segment(BaseModel):
    """Виртуальный пиксель: часть ленты под контролем одного контроллера.

    Прошивка получает start/length и знает, что остальным рулят другие узлы.
    """

    device_id: str
    start: int = Field(ge=0)       # первый физический пиксель сегмента
    length: int = Field(gt=0)
    # Координата начала сегмента внутри общего виртуального холста стенда.
    canvas_offset: int = 0


class Telemetry(BaseModel):
    """Данные сенсоров, которые эффекты могут потреблять на лету.

    Например уровень/частота звука одного устройства, синхронизируемого со стеной.
    """

    device_id: str
    kind: str                       # audio_level | audio_band | lux | motion ...
    value: float
    meta: dict = Field(default_factory=dict)  # {"band_hz": 120.0}
    ts: float = 0.0


class NativeRef(BaseModel):
    """Ссылка на родной эффект экосистемы + его нативные аргументы."""

    ecosystem: str
    effect: str
    args: dict = Field(default_factory=dict)


class PortResult(BaseModel):
    """Результат native(src) -> IR -> native(dst)."""

    ir: EffectIR
    target_native: Optional[NativeRef] = None
    degraded_to: Optional[EffectIR] = None   # если цель не умеет класс — композиция
    unsupported: bool = False
    notes: list[str] = Field(default_factory=list)
