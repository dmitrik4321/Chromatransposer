"""Протокол GyverLamp2 (UDP) — по материалам сообщества.

Источник истины: smeleshkin/GyverLamp2HA (README/docs/protocol.md, проверено
на живом репозитории). Из прошивки AlexGyver/GyverLamp2 (коммит 145fbfb,
объявляет 0.23b) взяты только форматы пакетов; код HA-интеграции не копировался.

Ключевые факты протокола (см. docs/protocol.md в GyverLamp2HA):
  * Транспорт — UDP, без ACK/аутентификации/шифрования.
  * Порт = 50000 + hash(key)*... % 15000 + group; для GL/1 => 61197.
    port=17; для символа ключа port=(port*ASCII(char)) & 0xffff.
  * Пакеты ASCII CSV с префиксом "GL":
      GL_ONL<n>            heartbeat от лампы каждые 500 мс (broadcast на порт группы)
      GL,0,<cmd>,d,h,m,s   power off/on (0/1), prev/next preset (4/5);
                           побочный эффект — установка часов, если нет NTP
      GL,4,1,n             выбрать пресет n (0..39) — роль Slave
      GL,4,2,b             глобальная яркость 1..255 — роль Slave (0 => выкл.)
      GL,2,N,<13f>*N,<slot>,d,h,m,s   полная запись банка из N режимов (<=19)
  * Поля режима (13 байт): effect, fadeBright, bright, advMode, soundReact,
    min, max, speed, palette, scale, fromCenter, color, fromPal.
  * Эффекты прошивки: 1 perlin, 2 color, 3 cycle, 4 gradient, 5 particles,
    6 fire, 7 fire2020, 8 confetti, 9 tornado, 10 clock.
  * Палитры встроенные id 2..32 (2=heat ... 12=rainbow ... 32=aurora).
  * Throttle прошивки 500 мс; Android ретранслит команду 6 раз по 8 мс —
    интеграция вместо этого сериализует отправки и ждёт >=650 мс.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import IntEnum
from typing import Optional

PACKET_PREFIX = "GL"
ENCODING = "ascii"

# --- ограничения прошивки -----------------------------------------------------
MAX_PRESETS = 40            # слотов пользовательских пресетов
MAX_MODE_RECORDS = 19       # dataStart — byte, >=20 переполняется
MODE_FIELDS = 13
HEARTBEAT_MS = 500          # лампа шлёт GL_ONL каждые 500 мс
SEND_MIN_INTERVAL = 0.65    # сериализация команд: >=650 мс между отправками


class PacketType(IntEnum):
    CONTROL = 0
    CONFIG = 1              # полная конфигурация — не используем (опасно)
    MODE_BANK = 2
    ALARM = 3               # будильники — оставлены приложению
    SLAVE = 4
    PALETTE = 5             # палитры — оставлены приложению


class ControlCommand(IntEnum):
    OFF = 0
    ON = 1
    PREVIOUS = 4
    NEXT = 5


class SlaveCommand(IntEnum):
    PRESET = 1
    BRIGHTNESS = 2


class FirmwareEffect(IntEnum):
    PERLIN = 1
    COLOR = 2
    CYCLE = 3
    GRADIENT = 4
    PARTICLES = 5
    FIRE = 6
    FIRE_2020 = 7
    CONFETTI = 8
    TORNADO = 9
    CLOCK = 10


EFFECT_KEYS = {  # id прошивки -> машинное имя (для capability registry)
    int(FirmwareEffect.PERLIN): "perlin",
    int(FirmwareEffect.COLOR): "color",
    int(FirmwareEffect.CYCLE): "cycle",
    int(FirmwareEffect.GRADIENT): "gradient",
    int(FirmwareEffect.PARTICLES): "particles",
    int(FirmwareEffect.FIRE): "fire",
    int(FirmwareEffect.FIRE_2020): "fire2020",
    int(FirmwareEffect.CONFETTI): "confetti",
    int(FirmwareEffect.TORNADO): "tornado",
    int(FirmwareEffect.CLOCK): "clock",
}
KEY_TO_EFFECT = {v: k for k, v in EFFECT_KEYS.items()}

# Встроенные палитры 2..32 (порядок = paletteArr из palettes.h прошивки;
# сверено с palettes.py интеграции GyverLamp2HA).
PALETTES = (
    "heat", "fire", "wood_fire", "normal_fire", "lithium_fire", "sodium_fire",
    "copper_fire", "alcohol_fire", "lava", "party", "rainbow", "rainbow_stripe",
    "cloud", "ocean", "forest", "sunset", "blue_red", "optimus_prime", "warm",
    "cold", "hot", "pink", "comfy", "cyberpunk", "girl", "christmas", "acid",
    "blue_smoke", "gummy", "leo", "aurora",
)
PALETTE_FIRST_ID = 2
HEAT_PALETTE_ID = 2
RAINBOW_PALETTE_ID = 12


def palette_id(value: str | int) -> int:
    if isinstance(value, int) and PALETTE_FIRST_ID <= value < PALETTE_FIRST_ID + len(PALETTES):
        return value
    for i, key in enumerate(PALETTES, PALETTE_FIRST_ID):
        if value == key:
            return i
    raise ValueError(f"неизвестная встроенная палитра: {value!r} (доступны 2..32)")


def palette_name(pid: int) -> str:
    return PALETTES[pid - PALETTE_FIRST_ID]


class ModeField(IntEnum):
    EFFECT = 0
    FADE_BRIGHT = 1
    BRIGHTNESS = 2
    ADV_MODE = 3
    SOUND_REACT = 4
    REACT_MIN = 5
    REACT_MAX = 6
    SPEED = 7
    PALETTE = 8
    SCALE = 9
    FROM_CENTER = 10
    HUE = 11
    USE_PALETTE = 12


# --- расчёт порта ---------------------------------------------------------------

def udp_port(group: int = 1, key: str = "GL") -> int:
    """port=17; для символа ключа port=(port*ASCII)&0xffff; затем 50000+port%15000+group."""
    if not (0 <= group <= 9999):
        raise ValueError("группа вне диапазона")
    if not (1 <= len(key) <= 10) or not all(33 <= ord(c) <= 126 for c in key):
        raise ValueError("ключ: 1..10 печатных ASCII")
    h = 17
    for ch in key:
        h = (h * ord(ch)) & 0xFFFF
    return 50000 + (h % 15000) + group


def firmware_weekday(now: datetime) -> int:
    """В прошивке воскресенье = 0."""
    return (now.weekday() + 1) % 7


_HEARTBEAT = re.compile(rb"GL_ONL([0-9]{1,2})")


def parse_heartbeat(data: bytes) -> Optional[int]:
    """GL_ONL<n> -> индекс текущего пресета (0..39), иначе None."""
    m = _HEARTBEAT.fullmatch(data.strip())
    if m and int(m.group(1)) < MAX_PRESETS:
        return int(m.group(1))
    return None


# --- конструкторы пакетов ---------------------------------------------------------

def _time_fields(now: datetime) -> tuple[int, int, int, int]:
    return (firmware_weekday(now), now.hour, now.minute, now.second)


def control_packet(cmd: ControlCommand, now: Optional[datetime] = None) -> bytes:
    now = now or datetime.now()
    d, hh, mm, ss = _time_fields(now)
    return f"{PACKET_PREFIX},{PacketType.CONTROL},{int(cmd)},{d},{hh},{mm},{ss}".encode(ENCODING)


def slave_brightness(value: int) -> bytes:
    if not 1 <= value <= 255:
        raise ValueError("яркость Slave 1..255; для нуля — команда выключения")
    return f"{PACKET_PREFIX},{PacketType.SLAVE},{SlaveCommand.BRIGHTNESS},{value}".encode(ENCODING)


def slave_preset(index: int) -> bytes:
    if not 0 <= index < MAX_PRESETS:
        raise ValueError(f"пресет 0..{MAX_PRESETS - 1}")
    return f"{PACKET_PREFIX},{PacketType.SLAVE},{SlaveCommand.PRESET},{index}".encode(ENCODING)


def validate_mode_record(row: tuple[int, ...]) -> None:
    if len(row) != MODE_FIELDS or not all(isinstance(v, int) and 0 <= v <= 255 for v in row):
        raise ValueError(f"режим — ровно {MODE_FIELDS} байтовых полей")
    if not (FirmwareEffect.PERLIN <= row[ModeField.EFFECT] <= FirmwareEffect.CLOCK):
        raise ValueError("effect id вне 1..10")
    if row[ModeField.FADE_BRIGHT] not in (0, 1):
        raise ValueError("fadeBright 0/1")
    if not 1 <= row[ModeField.ADV_MODE] <= 5:
        raise ValueError("advMode 1..5")
    if not 1 <= row[ModeField.SOUND_REACT] <= 3:
        raise ValueError("soundReact 1..3")
    if row[ModeField.REACT_MIN] > row[ModeField.REACT_MAX]:
        raise ValueError("reaction min > max")
    if not PALETTE_FIRST_ID <= row[ModeField.PALETTE] <= PALETTE_FIRST_ID + len(PALETTES) - 1:
        raise ValueError("palette id вне 2..32")
    if row[ModeField.FROM_CENTER] not in (0, 1) or row[ModeField.USE_PALETTE] not in (0, 1):
        raise ValueError("fromCenter/usePalette 0/1")


def mode_bank_packet(records: list[tuple[int, ...]], selected: int,
                     now: Optional[datetime] = None) -> bytes:
    """GL,2,N,<13*N полей>,slot+1,d,h,m,s — полная замена банка режимов."""
    if not 1 <= len(records) <= MAX_MODE_RECORDS:
        raise ValueError(f"банка: 1..{MAX_MODE_RECORDS} режимов")
    if not 0 <= selected < len(records):
        raise ValueError("выбранный слот вне банка")
    now = now or datetime.now()
    fields = [PACKET_PREFIX, PacketType.MODE_BANK, len(records)]
    for row in records:
        validate_mode_record(row)
        fields.extend(row)
    fields.append(selected + 1)  # сдвиг слота в прошивке: slot от 1
    fields.extend(_time_fields(now))
    packet = ",".join(map(str, fields)).encode(ENCODING)
    if len(packet) > 1460:
        raise ValueError("пакет не влезает в один UDP дейтаграмму")
    return packet
