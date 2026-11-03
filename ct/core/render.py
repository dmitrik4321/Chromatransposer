"""ASCII-логгер: что должно было вывестиься на ленту, цветом, поблочно.

Важно про границу проекта: прослойка — control plane, эффекты считает
прошивка устройства. Этот модуль НИКОГО не обманывает: он показывает
ПРЕДПОЛОЖЕНИЕ («что должно быть на ленте»), вычисленное сервером по тому
же Effect IR, который уходит на юг. Точность зависит от честности
адаптеров; расхождение с реальностью — повод чинить маппинг, а не
доверять картинке слепо.

Ключевые фичи (требование пользователя):
  * цветной ASCII (символ = яркость пикселя, цвет = сам пиксель);
  * синхронизация кадров по общему тиковому времени (решение B) — все
    узлы стенда рендерятся от одного wall-clock t, картинка стенки
    согласована;
  * новый кадр СТИРАЕТ предыдущий в собственной зоне вывода (ANSI),
    ввод команды при этом не мешает: REPL рисует prompt ниже зоны
    рендера, после каждого перерисовываемого кадра prompt возвращается;
  * классификация по типу устройства: strip / matrix / tree. Для «ёлки»
    лента намотана около-хаотично, поэтому человек видит НЕ линию, а
    проекцию на конус: символ выбирается из строки конуса по доле длины
    сегмента — это то, что реально замечает глаз, а не индексы LED.
"""
from __future__ import annotations

import math
import time
from typing import Optional

from ct.core.ir import EffectIR, EffectParams, Palette

# ------------------------------ КОНФИГ РЕНДЕРА --------------------------------
# Всё меняемое — здесь; переопределяется секцией `render:` в configs/server.yaml.
RENDER_CONFIG = {
    # символы яркости от темного к светлому (8 градаций)
    "ramp": " .:-=+█",
    "fps": 10.0,                    # частота перерисовки консоли
    "cell_width": 2,                # ширины символов на пиксель (2 = квадратнее)
    "max_canvas_px": 512,           # потолок ширины холста в символах ( downsamples )
    "dim_offline": True,            # офлайн-узлы показывать гашёными
    "show_losses": True,            # дописывать строку потерь трансляции
    # профили проекций для нестандартных форм (типы устройств)
    "tree_profile": {
        "rows": 16,                 # высота конуса в символах
        "apex_frac": 0.0,           # доля длины ленты на самом верху (0 = острие)
        "base_width": 15,           # ширина основания в символах
    },
    # профиль тайлов для типа screen (браузер/монитор): плитки «пикселей»
    "screen_profile": {"cols": 4, "label_w": 12},
    # эвристики типа устройства по ключевым словам id/name/labels
    "type_heuristics": {"tree": ["ёлк", "елк", "tree", "garland"],
                          "screen": ["opera", "browser", "монитор", "tv"]},
}


def configure(cfg: dict) -> None:
    """Переопределение RENDER_CONFIG из YAML-секции `render:` (конфиг как код)."""
    for k, v in cfg.items():
        if k in RENDER_CONFIG and isinstance(v, dict) and isinstance(RENDER_CONFIG[k], dict):
            RENDER_CONFIG[k].update(v)
        else:
            RENDER_CONFIG[k] = v

# ------------------------------ КЛАССИФИКАЦИЯ ТИПОВ ---------------------------
# Тип устройства — отдельная каноническая ось besides топологии: форма монтажа
# влияет на то, ЧТО УВИДИТ человек. Определяется: явным labels["type"], иначе
# по эвристике (см. classify_device). Расширяется без правки кода — через
# RENDER_CONFIG["type_heuristics"] и profile-таблицу ниже.
DEVICE_TYPES = ("strip", "matrix", "tree", "screen")

TYPE_PROFILES: dict[str, str] = {
    "strip": "linear",       # развёртка слева направо
    "matrix": "grid",        # WxH сетка
    "tree": "cone",          # спираль на конус: human view = rows*cols
    "screen": "tiles",       # дискретные «пиксели» произвольной формы (вкладки браузера)
}


def classify_device(node, config: Optional[dict] = None) -> str:
    """Тип устройства: strip | matrix | tree.

    Приоритет: node.device_type (явно задан человеком — всегда честнее
    эвристики), затем labels['type'], затем эвристики из конфига, затем
    вывод из топологии.
    """
    cfg = config or RENDER_CONFIG
    if node.device_type in DEVICE_TYPES:
        return node.device_type
    explicit = node.labels.get("type")
    if explicit in DEVICE_TYPES:
        return explicit
    hay = f"{node.id} {node.name} {node.labels}".lower()
    for dtype, keywords in cfg.get("type_heuristics", {}).items():
        if any(k in hay for k in keywords):
            return dtype
    if node.topology.is_matrix:
        return "matrix"
    return "strip"


# ------------------------------ ВЫЧИСЛЕНИЕ КАДРА ------------------------------
# Мини-интерпретатор IR: ровно те же формулы, что объявлены в firmware/ct_ir.h
# (здесь — эталон для предпросмотра; прошивка вправе считать точнее).
# Все классы параметризованы нормализованными 0..1 значениями.

def _hex_to_rgb(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _palette_color(palette, u: float) -> tuple[int, int, int]:
    """u в [0,1) по градиенту стопов; пустая палитра = белым."""
    stops = palette.stops
    if not stops:
        return 255, 255, 255
    if len(stops) == 1:
        return _hex_to_rgb(stops[0].color)
    u %= 1.0
    for i in range(len(stops) - 1):
        a, b = stops[i], stops[i + 1]
        if a.pos <= u <= b.pos:
            span = max(1e-9, b.pos - a.pos)
            f = (u - a.pos) / span
            ca, cb = _hex_to_rgb(a.color), _hex_to_rgb(b.color)
            return tuple(round(ca[k] + (cb[k] - ca[k]) * f) for k in range(3))
    return _hex_to_rgb(stops[-1].color)


def _hash_noise(i: int, seed: int = 0) -> float:
    """Детерминированный псевдослучайный шум 0..1 (одинаков на всех узлах)."""
    x = math.sin((i * 127.1 + seed * 311.7)) * 43758.5453
    return x - math.floor(x)


def _smooth_noise(x: float) -> float:
    """Линейно интерполированный value-noise — дешёвая замена perlin."""
    i0 = math.floor(x)
    f = x - i0
    a, b = _hash_noise(int(i0)), _hash_noise(int(i0) + 1)
    s = f * f * (3 - 2 * f)          # smoothstep
    return a + (b - a) * s


def effect_pixel(cls: str, params, u: float, v: float, t: float,
                 length: int) -> tuple[float, float]:
    """Возвращает (позиция в палитре 0..1, множитель яркости 0..1).

    u,v — нормализованные координаты пикселя (для 1D v=0); t — общее
    время секунд (sync-tick), length — число пикселей узла.
    """
    speed = params.speed
    phase = t * (0.05 + speed * 2.0)
    scale = 0.05 + params.scale * 1.5
    density = params.density

    if cls == "static":
        return 0.0, 1.0
    if cls == "blink":
        on = math.sin(phase * math.pi * 2) > 0
        return 0.0, 1.0 if on else 0.0
    if cls == "breathing":
        return 0.0, 0.5 + 0.5 * math.sin(phase * math.pi * 2)
    if cls == "rainbow":
        return u + phase * 0.2, 1.0
    if cls == "colorsweep":
        return u * scale - phase, 1.0
    if cls == "perlin":
        n = _smooth_noise(u * length * scale * 0.3 + phase)
        return n, 0.3 + 0.7 * n
    if cls == "plasma":
        p = (math.sin(u * scale * 6 + phase * 2)
             + math.sin(v * scale * 6 - phase)
             + math.sin((u + v) * scale * 3 + phase)) / 3.0
        return (p + 1) / 2, 1.0
    if cls == "fire":
        flick = _smooth_noise(u * length * scale * 0.5 - t * (2 + speed * 8))
        heat = max(0.0, 1.0 - v) * (0.5 + 0.5 * flick)
        return 1.0 - heat, min(1.0, heat * 1.6)
    if cls == "rain":
        drop = _hash_noise(int(u * length * (1 + density * 6)))
        head = ((t * (1 + speed * 3) + drop * 17) % 1.0)
        d = abs(head - v)
        glow = max(0.0, 1.0 - d * (4 + density * 20))
        return 0.65, glow
    if cls == "twinkle":
        tw = _hash_noise(int(u * length) + int(t * (1 + speed * 6)))
        return 0.15, 1.0 if tw > (1 - density * 0.6) else 0.08
    if cls == "composition":
        # деградационная композиция: просто мягкая волна по палитре
        return u * scale + phase * 0.3, 0.6 + 0.4 * math.sin(phase)
    if cls == "sparkle":
        # gyverlamp2 confetti/particles: редкие вспышки случайных пикселей
        tw = _hash_noise(int(u * length) + int(t * (2 + speed * 8)))
        thr = 1 - 0.08 - density * 0.25
        return 0.75, 1.0 if tw > thr else 0.05
    if cls == "swirl":
        # gyverlamp2 tornado/cycle: вращающийся градиент по углу/длине
        ang = (u + v * 0.5) * 2 - phase * 0.6
        return ang % 1.0, 0.7 + 0.3 * math.sin(ang * math.pi * 2)
    if cls == "clock":
        # gyverlamp2 clock: время как цвет; для ASCII это статичная плашка
        h = (t / 3600.0) % 1.0
        return h, 0.9
    if cls == "gradient":
        # двухполосный градиент с медленным дрейфом
        g = abs(((u + phase * 0.1) % 1.0) * 2 - 1)
        return g, 0.6 + 0.4 * g
    # custom/opaque — нейтральная волна, честно помечается потерей выше
    return u + phase * 0.1, 0.8


# ------------------------------ ПРОЕКЦИИ (human view) -------------------------

def project_tree(length: int, idx: int, rows: int, base_width: int,
                 apex_frac: float = 0.0) -> tuple[int, int]:
    """Индекс LED на ёлке -> (row, col) того, что видит человек.

    Лента намотана около-хаотично по конусу: считаем, что она идёт снизу
    вверх витками; row делит высоту конуса, col — место в ряду. Это
    приближение, но именно оно соответствует восприятию (глаз видит силуэт
    ёлки, а не порядок адресов). Профиль меняется из RENDER_CONFIG.
    """
    frac = idx / max(1, length - 1)               # положение вдоль ленты 0..1
    row = min(rows - 1, int(frac * rows))         # низ->верх или наоборот?
    row = rows - 1 - row                          # нулевой ряд = макушка
    width_at_row = max(1, round(base_width * (row + 1) / rows))
    # внутри ряда — равномерная раскладка оставшихся пикселей витка
    per_row = max(1, math.ceil(length / rows))
    pos_in_row = (idx % per_row)
    col = int(pos_in_row / max(1, per_row - 1) * (width_at_row - 1)) if per_row > 1 else 0
    return row, col


# ------------------------------ ЗОНА ВЫВОДА В КОНСОЛИ -------------------------

class RenderZone:
    """Рисует кадры в своей полосе терминала, стирая предыдущий кадр.

    Работает поверх Rich Console: Rich пишет после нас, поэтому каждая
    перерисовка возвращает каретку в начало зоны. Ввод пользователя не
    блокируется — REPL вызывает render() из фонового task, а prompt
    перерисовывается сразу после кадра.
    """

    def __init__(self, console, height_rows: int, *, config: Optional[dict] = None):
        self.console = console
        self.height = height_rows
        self.cfg = config or RENDER_CONFIG
        self._last_lines = 0
        self.active = False

    def erase(self) -> None:
        """Вернуть курсор наверх зоны и затереть строки (ANSI EL)."""
        if self._last_lines:
            self.console.file.write(f"\x1b[{self._last_lines}F")
            for _ in range(self._last_lines):
                self.console.file.write("\x1b[K\x1b[1E")
            self.console.file.write(f"\x1b[{self._last_lines}F")
            self.console.file.flush()

    def draw(self, lines: list[str]) -> None:
        self.erase()
        for ln in lines:
            self.console.file.write(ln + "\n")
        self._last_lines = len(lines)
        self.console.file.flush()


# ------------------------------ КАНВАС СТЕНДА / ОДИНОЧНОГО УЗЛА ---------------

def _rgb_cell(r: int, g: int, b: int, lum: float, ramp: str,
              cell_width: int, dim: bool = False) -> str:
    """Один пиксель -> ANSI-блок: цвет = RGB, яркость = символ в блоке."""
    idx = min(len(ramp) - 1, max(0, round(lum * (len(ramp) - 1))))
    ch = ramp[idx] * cell_width
    if dim:
        r, g, b = r // 3, g // 3, b // 3
    return f"\x1b[38;2;{r};{g};{b}m{ch}\x1b[0m"


def render_device_frame(node, ir: Optional[EffectIR], t: float,
                        config: Optional[dict] = None,
                        seg_map: Optional[dict] = None) -> list[str]:
    """Строки ASCII для одного узла. t — общее время (сек), один и тот же
    для всех участников синхронного стенда (решение B)."""
    cfg = config or RENDER_CONFIG
    ramp = cfg["ramp"]
    cw = cfg["cell_width"]
    dtype = classify_device(node, cfg)
    top = node.topology
    params = ir.params if ir else EffectParams(brightness=0.0)
    palette = ir.palette if ir else Palette()
    cls = ir.class_.value if ir else "static"
    on = bool(node.online.value == "online") and (params.brightness > 0)
    lines: list[str] = []

    header = f"{node.id} [{dtype}]"
    if ir is None:
        header += " (нет desired IR — показываю выключенным)"
    lines.append(header)

    if not on and cfg["dim_offline"]:
        # гашёный кадр: всё нулями, но топология видна
        blank = "\x1b[90m" + (" " * cw) + "\x1b[0m"
        if dtype == "matrix":
            for _ in range(top.height):
                lines.append(blank * top.width)
        elif dtype == "tree":
            tp = cfg["tree_profile"]
            for r in range(tp["rows"]):
                w = max(1, round(tp["base_width"] * (r + 1) / tp["rows"]))
                pad = (tp["base_width"] - w) // 2
                lines.append(" " * (cw * pad) + blank * w)
        else:
            lines.append(blank * min(top.length, cfg["max_canvas_px"]))
        return lines

    def px(u: float, v: float, length: int) -> str:
        pu, lum = effect_pixel(cls, params, u, v, t, length)
        r, g, b = _palette_color(palette, pu)
        bri = params.brightness
        return _rgb_cell(int(r * bri), int(g * bri), int(b * bri),
                         lum, ramp, cw)

    if dtype == "matrix":
        for row in range(top.height):
            line = "".join(px(c / max(1, top.width - 1),
                              row / max(1, top.height - 1), top.length)
                           for c in range(top.width))
            lines.append(line)
    elif dtype == "tree":
        tp = cfg["tree_profile"]
        rows, bw = tp["rows"], tp["base_width"]
        grid: dict[tuple[int, int], str] = {}
        n = min(top.length, cfg["max_canvas_px"])
        for i in range(n):
            r, c = project_tree(n, i, rows, bw, tp.get("apex_frac", 0.0))
            frac_u = i / max(1, n - 1)
            grid[(r, c)] = px(frac_u, 1.0 - r / max(1, rows - 1), n)
        for r in range(rows):
            cells = []
            for c in range(bw):
                if (r, c) in grid:
                    cells.append(grid[(r, c)])
                else:
                    # пустое место ряда — отступ по центру конуса
                    width_at_row = max(1, round(bw * (r + 1) / rows))
                    pad = (bw - width_at_row) // 2
                    if c < pad or c >= bw - pad:
                        cells.append(" " * cw)
                    else:
                        cells.append(_rgb_cell(0, 0, 0, 0, ramp, cw, dim=True))
            lines.append("".join(cells))
    elif dtype == "screen":
        # «пиксели» произвольной формы (вкладки браузера): тайлы с подписями.
        # Что увидит человек — дискретные плитки, а не линия; u = доля индекса.
        scfg = cfg.get("screen_profile", {"cols": 4, "label_w": 12})
        cols = scfg["cols"]
        labels = node.labels.get("screen_titles", "")
        labels = labels.split("\x1f") if labels else []   # разделитель из адаптера
        n = max(len(labels), min(top.length, cfg["max_canvas_px"]), 1)
        row: list[str] = []
        for i in range(n):
            cell = px(i / max(1, n - 1), 0.0, n)
            label = (labels[i][:scfg["label_w"]] if i < len(labels) else f"#{i}")
            label = label.ljust(scfg["label_w"])[:scfg["label_w"]]
            row.append(cell + "\x1b[90m" + label + "\x1b[0m ")
            if len(row) == cols:
                lines.append("".join(row))
                row = []
        if row:
            lines.append("".join(row))
    else:  # strip: human view с учётом виртуальных сегментов (canvas_offset)
        total = sum(s.length for s in (seg_map.values() or [] if seg_map else [])) \
            if seg_map else 0
        canvas_len = max(total, top.length, 1)
        n = min(top.length, cfg["max_canvas_px"])
        seg = (seg_map or {}).get(node.id)
        off = seg.canvas_offset if seg else 0
        pre = " " * cw * min(off, cfg["max_canvas_px"])
        line = "".join(px((off + i) / canvas_len, 0.0, n) for i in range(n))
        lines.append(pre + line)

    if cfg["show_losses"] and ir and ir.losses:
        lines.append("\x1b[33mпотери: " + "; ".join(ir.losses) + "\x1b[0m")
    return lines


class FrameLogger:
    """Фоновый ASCII-логгер для CLI: общий тик, стирание старого кадра.

    Хранит desired-IR по устройствам (берётся из оркестратора/реестра через
    snapshot_fn), рендерит стенд целиком или одно устройство.
    """

    def __init__(self, console, zone_height: int = 24,
                 config: Optional[dict] = None):
        self.console = console
        self.cfg = config or RENDER_CONFIG
        self.zone = RenderZone(console, zone_height, config=self.cfg)
        self.enabled = False
        self._task = None

    async def start(self, snapshot_fn, only_device: Optional[str] = None) -> None:
        """snapshot_fn() -> list[(node, ir_or_None, seg_map)]; вызывается на кадр."""
        import asyncio
        if self.enabled:
            return
        self.only_device = only_device
        self.enabled = True
        self._task = asyncio.create_task(self._loop(snapshot_fn))

    async def stop(self) -> None:
        import asyncio
        self.enabled = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.zone.erase()

    async def _loop(self, snapshot_fn) -> None:
        import asyncio
        period = 1.0 / max(1.0, self.cfg["fps"])
        while self.enabled:
            lines: list[str] = []
            try:
                for node, ir, segs in snapshot_fn():
                    if self.only_device and node.id != self.only_device:
                        continue
                    lines += render_device_frame(node, ir, time.time(),
                                                 self.cfg, segs)
                    lines.append("")
            except Exception as e:  # не роняем REPL из-за предпросмотра
                lines = [f"\x1b[31mrender error: {e}\x1b[0m"]
            self.zone.draw(lines[: self.zone.height + 8])
            await asyncio.sleep(period)
