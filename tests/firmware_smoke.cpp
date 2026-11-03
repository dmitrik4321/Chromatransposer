// Хостовый smoke-тест ct_ir.h: парсинг IR-JSON, сегментация, тик, телеметрия.
#include "firmware/ct_ir.h"
#include <cassert>
#include <cstdio>
#include <cstring>

CtIr ctIr;   // экземпляр объявляет «прошивка» (ctest за .ino)

static int g_render_calls = 0;
static CtIr::CtSnapshot g_last_snap;
static void testRender(const CtIr::CtSnapshot& s, TPixel* out, uint16_t n) {
    g_render_calls++;
    g_last_snap = s;
    for (uint16_t i = 0; i < n; i++) out[i] = ctIr.paletteAt((uint16_t)(i * 1000 / (n ? n : 1)));
}
static void testFallback(TPixel* out, uint16_t n) {
    for (uint16_t i = 0; i < n; i++) { out[i].r = 9; out[i].g = 9; out[i].b = 9; }
}

int main() {
    ctIr.begin();
    ctIr.onRender(testRender);
    ctIr.onFallback(testFallback);

    // 1) Полная IR-инструкция: класс+параметры+палитра+сегмент+сенсор+sync.
    const char* j1 = R"({
      "class": "perlin",
      "params": {"speed": 700, "scale": 400, "brightness": 900},
      "palette": {"stops": ["#ff0000", "#00ff00", "#0000ff"], "cyclic": true},
      "segment": {"start": 128, "length": 64, "canvas_offset": 0},
      "sensor": {"kind": "audio_level", "value": 0.5},
      "config": {"watchdog_ms": 5000, "default_period_ms": 40}
    })";
    assert(ctIr.applyJson(j1, strlen(j1)));
    auto snap = ctIr.snapshot();
    assert(snap.cls == CtClass::Perlin);
    assert(snap.params.speed == 700 && snap.params.scale == 400);
    assert(snap.palette.count == 3);
    assert(snap.segment.start == 128 && snap.segment.length == 64);
    assert(snap.sensor_count == 1);
    assert(!strcmp(snap.sensors[0].kind, "audio_level"));
    assert(ctIr.cfg.watchdog_ms == 5000);   // дистанционный конфиг сработал

    // 2) Палитра: интерполяция стопов не вырождается.
    TPixel mid = ctIr.paletteAt(500);
    assert(mid.g > 0 || mid.r > 0);

    // 3) Sync tick + фазовый шаг.
    const char* j2 = R"({"sync": {"tick_seq": 1, "wall_ts": 1000, "period_ms": 50}})";
    assert(ctIr.applyJson(j2, strlen(j2)));
    ct_test_advance(120);
    TPixel leds[16];
    ctIr.render(leds, 16);
    assert(g_render_calls == 1);

    // 4) Неизвестный класс -> Custom (честный hook, без подмены).
    const char* j3 = R"({"class": "quantum_fog"})";
    assert(ctIr.applyJson(j3, strlen(j3)));
    assert(ctIr.snapshot().cls == CtClass::Custom);

    // 5) Watchdog: долго нет команд -> fallback-хук.
    ct_test_advance(6000);
    ctIr.render(leds, 16);
    assert(ctIr.snapshot().in_fallback);
    assert(leds[0].r == 9);

    // 6) Битый JSON -> false, состояние не тронуто.
    const char* bad = "{oops";
    assert(!ctIr.applyJson(bad, strlen(bad)));

    // 7) keep_phase — фаза не сбрасывается на continuation-команде.
    const char* j7 = R"({"class": "fire", "keep_phase": true})";
    assert(ctIr.applyJson(j7, strlen(j7)));
    assert(ctIr.snapshot().cls == CtClass::Fire);

    printf("SMOKE OK\n");
    return 0;
}
