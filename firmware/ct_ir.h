// ============================================================================
// ct_ir.h — IR-интерпретатор Chromatransposer для ESP-прошивок (data plane).
//
// НАЗНАЧЕНИЕ
//   Единый модуль, который любая конечная прошивка (WLED-форк, форк GyverHub,
//   native_ct) подключает одним include + одним вызовом CtIr::applyJson().
//   Модуль умеет:
//     1. Принимать JSON-инструкцию эффекта (EffectIR) по WiFi (MQTT/HTTP).
//     2. Принимать сегментацию: с какого ВИРТУАЛЬНОГО пикселя начинается лента
//        этого контроллера (другой частью рулят другие узлы стенда).
//     3. Принимать телеметрию синхронизированных устройств (например
//        audio_level/audio_band с узла, у которого есть микрофон).
//     4. Принимать sync tick (общая фаза анимации) и считать время effects
//        от него, а не от millis() — стены из разных ESP не разъезжаются.
//   Рендер эффектов — родной код прошивки. Прослойка НИКОГДА не шлёт
//   пиксельные буферы; сюда приходит только параметрическая схема.
//
// СТИЛЬ (по договорённости)
//   * Никакого хардкода: ВСЁ, что может потребоваться изменить, собрано
//     в одном конфиг-блоке ниже (struct CtIrConfig). Значения по умолчанию
//     можно переопределить до инициализации или прислать из сервера в поле
//     "config" того же JSON (сервер — единственный источник истины).
//   * Модуль исходников экосистем НЕ изменяет: он либо компилируется вместе
//     с вашим кодом (native_ct), либо встраивается в форк минимальным
//     диффом — см. раздел «ИНТЕГРАЦИЯ» в конце файла. Анализ диффа: один
//     include, одна точка входа handleCtJson(), три хука-обёртки.
//
// ЗАВИСИМОСТИ
//   * ArduinoJson >= 6.x (как в WLED и Gyver-прошивках).
//   * FastLED (для типов TPixel/CRGB) — если его нет, определите CT_NO_FASTLED
//     и предоставьте свою структуру пикселя (см. typedef TPixel ниже).
// ============================================================================

#ifndef CT_IR_H
#define CT_IR_H

#ifdef CT_HOST_TEST
// Хостовые smoke-тесты (tests/test_firmware_host.py): g++ + заглушки Arduino/
// ArduinoJson вместо ESP-окружения. Прошивки это не касается — макрос определён
// только в тестовой сборке.
#include "tests/host_stubs/Arduino.h"
#else
#include <Arduino.h>
#endif

#ifdef CT_NO_FASTLED
// Прошивка без FastLED: свой тип пикселя (RGB888-совместимый).
struct TPixel { uint8_t r = 0, g = 0, b = 0; };
#else
#include <FastLED.h>
typedef CRGB TPixel;
#endif

#ifdef CT_HOST_TEST
#include "tests/host_stubs/ArduinoJson.h"
#else
#include <ArduinoJson.h>
#endif
#include <string.h>

// ----------------------------------------------------------------------------
// ============================ КОНФИГ (ВСЁ МЕНЯЕМОЕ ТУТ) =====================
// Единственное место, где живут числа и строки, которые захочется править.
// Всё это можно переопределить из серверного JSON (секция "config"), чтобы
// не перепрошивать устройства ради изменения порога watchdog'а.
// ----------------------------------------------------------------------------
struct CtIrConfig {
    // --- Транспорт / топики (native_ct; для WLED-форка используются нативные
    //     HTTP/WS эндпоинты, эти строки нужны только MQTT-клиенту прошивки) ---
    const char* topic_prefix      = "ct/v1";        // каноническое дерево
    const char* topic_suffix_ir   = "/ir";          // EffectIR JSON
    const char* topic_suffix_set  = "/set";         // state/sensor/sync точечно
    const char* topic_sync_tick   = "ct/v1/sync/tick"; // общий тик стенда
    const char* topic_suffix_state     = "/state";  // retain-отчёт
    const char* topic_suffix_available = "/available"; // LWT offline

    // --- Адресация виртуального холста ----------------------------------------
    uint16_t max_physical_leds = 512;   // физический потолок ленты этой ESP
    uint16_t canvas_start      = 0;     // текущий offset: какой виртуальный
                                        // пиксель является нулём этой ленты
    uint16_t canvas_length     = 0;     // 0 = вся физическая лента видима

    // --- Синхронизация фазы ----------------------------------------------------
    uint16_t default_period_ms = 50;    // FPS ceiling по умолчанию (20 fps)
    int16_t  max_drift_ms      = 50;    // решение B: tolerance drift на tick
    uint16_t drift_correction_pct = 25; // % коррекции скорости при рассинхроне
    uint16_t tick_stale_ms     = 3000;  // нет тиков дольше -> свободный ход

    // --- Watchdog офлайна сервера ----------------------------------------------
    uint16_t watchdog_ms       = 30000; // нет команд дольше -> fallback-сцена
    bool     fallback_enabled  = true;  // рисовать ли fallback вместо last IR

    // --- Телеметрия ------------------------------------------------------------
    uint16_t sensor_ttl_ms     = 2000;  // данные с чужих узлов старше -> stale
    uint8_t  max_sensors       = 8;     // сколько видов телеметрии держим

    // --- JSON -------------------------------------------------------------------
    uint16_t json_capacity     = 2048;  // парсер <= 2 КБ (см. firmware/README)
};
// ----------------------------------------------------------------------------
// ========================== /КОНФИГ ===========================================
// ----------------------------------------------------------------------------

// ---- Родные классы эффектов (совпадают со строками EffectIR.class) ----------
// Plain enum (не enum class): значения нужны и как int для индексации имён.
enum CtClass : uint8_t {
    Off = 0, Static, Perlin, Plasma, Fire, Rain, Twinkle, Rainbow,
    Sweep, Gradient, Stripe, Custom,   // Custom — hook прошивки под своё
    COUNT
};

inline CtClass ctParseClass(const char* s) {
    if (!s) return CtClass::Custom;
    if (!strcmp(s, "off"))      return CtClass::Off;
    if (!strcmp(s, "static"))   return CtClass::Static;
    if (!strcmp(s, "perlin"))   return CtClass::Perlin;
    if (!strcmp(s, "plasma"))   return CtClass::Plasma;
    if (!strcmp(s, "fire"))     return CtClass::Fire;
    if (!strcmp(s, "rain"))     return CtClass::Rain;
    if (!strcmp(s, "twinkle"))  return CtClass::Twinkle;
    if (!strcmp(s, "rainbow"))  return CtClass::Rainbow;
    if (!strcmp(s, "sweep"))    return CtClass::Sweep;
    if (!strcmp(s, "gradient")) return CtClass::Gradient;
    if (!strcmp(s, "stripe"))   return CtClass::Stripe;
    return CtClass::Custom;  // неизвестный класс: честный hook, не подмена
}

inline const char* ctClassName(CtClass c) {
    static const char* names[] = {"off","static","perlin","plasma","fire",
        "rain","twinkle","rainbow","sweep","gradient","stripe","custom"};
    return names[min((uint8_t)c, (uint8_t)(CtClass::COUNT - 1))];
}

// ---- Параметры IR (нормализованные 0..1000 — сервер уже привёл шкалы) --------
struct CtParams {
    uint16_t speed      = 500;   // 0..1000
    uint16_t scale      = 500;   // пространственный масштаб
    uint16_t density    = 500;
    uint16_t blur       = 300;
    uint16_t brightness = 1000;  // поверх общей яркости прошивки
};

// ---- Палитра: упорядоченные стопы (<= 8; сервер обрезает лишние с потерей) ---
struct CtPalette {
    uint8_t  count = 0;
    TPixel   stops[8];
    bool     cyclic = true;
};

// ---- Сегментация виртуального холста ------------------------------------------
struct CtSegment {
    uint16_t start  = 0;      // виртуальный индекс начала этого контроллера
    uint16_t length = 0;      // 0 = вся физическая лента
    uint16_t canvas_offset = 0; // глобальный offset холста (для отладки)
};

// ---- Телеметрия чужих узлов (звук с микрофонного устройства и т.п.) ----------
struct CtSensor {
    char     kind[16];   // "audio_level", "audio_band", "humidity"...
    float    value;
    uint32_t recv_ms;
};

// ---- Sync tick -----------------------------------------------------------------
struct CtSync {
    uint32_t tick_seq;
    uint32_t wall_ts_ms;    // epoch ms сервера
    uint16_t period_ms;
    uint32_t local_ms;      // millis() на момент приёма
    bool     valid = false;
};

class CtIr {
public:
    CtIrConfig cfg;   // стартовые значения — из блока конфига выше

    // ---- Инициализация ---------------------------------------------------------
    void begin() {
        phase_ms_ = 0;
        last_cmd_ms_ = millis();
        in_fallback_ = false;
    }

    // Главный вход: строка JSON от сервера (топики ir/set или HTTP POST).
    // Возвращает false, если JSON не распарсился (сервер увидит это по state).
    bool applyJson(const char* json, size_t len) {
        JsonDocument doc;  // ArduinoJson v7: пул растёт сам, лимит — cfg.json_capacity
        DeserializationError err = deserializeJson(doc, json, len);
        if (err) return false;
        last_cmd_ms_ = millis();
        in_fallback_ = false;

        // Секция "config": сервер может дистанционно править наш конфиг-блок.
        if (JsonObjectHandle cfgj = doc["config"].as<JsonObject>()) {
            cfg.default_period_ms    = getU16(cfgj, "default_period_ms", cfg.default_period_ms);
            cfg.max_drift_ms         = getU16(cfgj, "max_drift_ms", cfg.max_drift_ms);
            cfg.drift_correction_pct = getU16(cfgj, "drift_correction_pct", cfg.drift_correction_pct);
            cfg.watchdog_ms          = getU16(cfgj, "watchdog_ms", cfg.watchdog_ms);
            cfg.sensor_ttl_ms        = getU16(cfgj, "sensor_ttl_ms", cfg.sensor_ttl_ms);
            cfg.canvas_start         = getU16(cfgj, "canvas_start", cfg.canvas_start);
            cfg.canvas_length        = getU16(cfgj, "canvas_length", cfg.canvas_length);
        }

        // EffectIR: {class, params{...}, palette{stops,cyclic}, segment{...}}
        if (doc["class"].is<const char*>()) {
            cls_ = ctParseClass(doc["class"]);
            JsonArrayHandle pj = doc["params"];
            params_.speed      = getU16(pj, "speed", params_.speed);
            params_.scale      = getU16(pj, "scale", params_.scale);
            params_.density    = getU16(pj, "density", params_.density);
            params_.blur       = getU16(pj, "blur", params_.blur);
            params_.brightness = getU16(pj, "brightness", params_.brightness);
            clampParams();

            JsonArrayHandle st = doc["palette"]["stops"];
            pal_.count = 0;
            pal_.cyclic = doc["palette"]["cyclic"].as<bool>(true);
            for (VariantHandle v : st) {
                if (pal_.count >= 8) break;               // сервер обязан предупредить
                uint32_t hex = parseColor(v);             // "#rrggbb" | 0xRRGGBB
                pal_.stops[pal_.count++] = pack(hex);
            }
            // Идемпотентность: новый baseline фазы только при новой инструкции
            // без continuation-флага (см. firmware/README требования).
            if (!doc["keep_phase"].as<bool>(false)) phase_ms_ = 0;
        }

        // Сегментация виртуальных пикселей.
        if (JsonObjectHandle sj = doc["segment"].as<JsonObject>()) {
            seg_.start         = getU16(sj, "start", seg_.start);
            seg_.length        = getU16(sj, "length", seg_.length);
            seg_.canvas_offset = getU16(sj, "canvas_offset", seg_.canvas_offset);
            cfg.canvas_start   = seg_.start;
            if (seg_.length) cfg.canvas_length = seg_.length;
        }

        // Teleметрия: {sensor:{kind,value}} или батч {sensors:[...]}
        ingestSensor(doc["sensor"]);
        for (VariantHandle s : doc["sensors"].as<JsonArray>()) ingestSensor(s);

        // Sync tick: {sync:{tick_seq,wall_ts,period_ms}}
        if (JsonObjectHandle y = doc["sync"].as<JsonObject>()) {
            CtSync t;
            t.tick_seq    = y["tick_seq"].as<uint32_t>();
            t.wall_ts_ms  = y["wall_ts"].as<uint32_t>();
            t.period_ms   = getU16(y, "period_ms", cfg.default_period_ms);
            t.local_ms    = millis();
            t.valid         = true;
            applyTick(t);
        }
        return true;
    }

    // Вызывать из loop() прошивки: отдаёт актуальный снимок состояния IR.
    // Прошивка сама решает, как рендерить (см. hooks ниже).
    void tick() {
        uint32_t now = millis();
        if (cfg.watchdog_ms && (now - last_cmd_ms_) > cfg.watchdog_ms)
            in_fallback_ = cfg.fallback_enabled;

        if (sync_.valid) {
            uint32_t elapsed = now - sync_.local_ms;
            if (elapsed > cfg.tick_stale_ms) {
                sync_.valid = false;           // сервер молчит — свободный ход
            } else {
                // фаза = базовая + прошедшие тики; дробный остаток даёт
                // интерполяцию между тиками (решение B: tolerance drift).
                float ticks = (float)elapsed / sync_.period_ms;
                phase_ms_ = base_phase_ms_ + (uint32_t)(ticks * cfg.default_period_ms);
            }
        } else {
            phase_ms_ += stepMs();             // локальные часы, free-run
        }
    }

    // ---------------- Хуки рендера (переопределить в прошивке) ----------------
    // Прошивка регистрирует callback'и; CtIr вызывает их из своего loop-кода.
    struct CtSnapshot {
        CtClass    cls;
        CtParams   params;
        CtPalette  palette;
        CtSegment  segment;
        uint32_t   phase_ms;
        CtSensor   sensors[8];
        uint8_t    sensor_count;
        bool       in_fallback;
    };

    using RenderFn = void (*)(const CtSnapshot&, TPixel* out, uint16_t n);
    using FallbackFn = void (*)(TPixel* out, uint16_t n);

    void onRender(RenderFn fn)        { render_fn_ = fn; }
    void onFallback(FallbackFn fn)    { fallback_fn_ = fn; }

    // Прокси: отрисовать кадр в буфер length физических пикселей.
    void render(TPixel* buf, uint16_t length) {
        tick();
        CtSnapshot snap = snapshot();
        if (snap.in_fallback && fallback_fn_) { fallback_fn_(buf, length); return; }
        if (render_fn_) render_fn_(snap, buf, length);
    }

    CtSnapshot snapshot() {
        CtSnapshot s;
        s.cls = cls_; s.params = params_; s.palette = pal_;
        s.segment = seg_; s.phase_ms = phase_ms_;
        s.in_fallback = in_fallback_;
        uint8_t k = 0;
        uint32_t now = millis();
        for (uint8_t i = 0; i < sensor_n_ && k < cfg.max_sensors; i++)
            if (now - sensors_[i].recv_ms <= cfg.sensor_ttl_ms)
                s.sensors[k++] = sensors_[i];
        s.sensor_count = k;
        return s;
    }

    // Помощники для хуков: цвет палитры по u∈[0..1000] с цикличностью.
    TPixel paletteAt(uint16_t u) const {
        if (pal_.count == 0) return pack(0xFFFFFF);
        if (pal_.count == 1) return pal_.stops[0];
        uint16_t span = pal_.cyclic ? pal_.count : (pal_.count - 1);
        uint32_t pos = (uint32_t)u * span / 1001;
        uint8_t i0 = pos % pal_.count, i1 = (pos + 1) % pal_.count;
        uint16_t frac = (pos % pal_.count) * 1000 / pal_.count;
        return blend(pal_.stops[i0], pal_.stops[i1], frac);
    }

private:
    CtClass  cls_ = CtClass::Off;
    CtParams params_;
    CtPalette pal_;
    CtSegment seg_;
    CtSensor  sensors_[8];
    uint8_t   sensor_n_ = 0;
    CtSync    sync_;
    uint32_t  phase_ms_ = 0, base_phase_ms_ = 0;
    uint32_t  last_cmd_ms_ = 0;
    bool      in_fallback_ = false;
    RenderFn   render_fn_   = nullptr;
    FallbackFn fallback_fn_ = nullptr;

    // Применение тика: считаем расхождение локальных часов и серверных.
    void applyTick(const CtSync& t) {
        cfg.default_period_ms = t.period_ms ? t.period_ms : cfg.default_period_ms;
        uint32_t server_elapsed = t.wall_ts_ms - base_wall_ms_;
        uint32_t local_elapsed  = t.local_ms - (sync_.valid ? sync_.local_ms : t.local_ms);
        int32_t drift = (int32_t)(server_elapsed) - (int32_t)(local_elapsed);
        if (abs(drift) > cfg.max_drift_ms) {
            // мягкая коррекция скорости (не скачок фазы): pct от периода
            int32_t corr = (drift * cfg.drift_correction_pct) / 100;
            base_phase_ms_ += corr;
        }
        base_wall_ms_ = t.wall_ts_ms;
        sync_ = t;
    }

    uint16_t stepMs() {
        // скорость IR 0..1000 -> шаг фазы; scale влияет на плотность узора
        uint32_t spd = params_.speed ? params_.speed : 1;
        return (uint16_t)(1 + (spd * 4) / 1000);   // 1..5 мс/loop — калибр proshivki
    }

    void clampParams() {
        uint16_t* p[] = {&params_.speed, &params_.scale, &params_.density,
                         &params_.blur, &params_.brightness};
        for (auto x : p) if (*x > 1000) *x = 1000;
    }

    void ingestSensor(JsonObjectHandle o) {
        if (!o) return;
        const char* kind = o["kind"].as<const char*>();
        if (!kind) return;
        uint8_t idx = sensor_n_;
        for (uint8_t i = 0; i < sensor_n_; i++)
            if (!strcmp(sensors_[i].kind, kind)) { idx = i; break; }
        if (idx == sensor_n_ && sensor_n_ < cfg.max_sensors) sensor_n_++;
        strncpy(sensors_[idx].kind, kind, sizeof(sensors_[idx].kind) - 1);
        sensors_[idx].kind[sizeof(sensors_[idx].kind) - 1] = 0;
        sensors_[idx].value   = o["value"].as<float>();
        sensors_[idx].recv_ms = millis();
    }

    static uint32_t parseColor(VariantHandle v) {
        if (v.is<const char*>()) {
            const char* s = v.as<const char*>();
            if (*s == '#') s++;
            return strtoul(s, nullptr, 16);
        }
        return v.as<uint32_t>();
    }
    static TPixel pack(uint32_t rgb) {
        TPixel p; p.r = (rgb >> 16) & 0xFF; p.g = (rgb >> 8) & 0xFF; p.b = rgb & 0xFF;
        return p;
    }
    static TPixel blend(const TPixel& a, const TPixel& b, uint16_t t0_1000) {
        TPixel p;
        p.r = a.r + (int16_t)(b.r - a.r) * t0_1000 / 1000;
        p.g = a.g + (int16_t)(b.g - a.g) * t0_1000 / 1000;
        p.b = a.b + (int16_t)(b.b - a.b) * t0_1000 / 1000;
        return p;
    }

    template <typename Obj>
    static uint16_t getU16(Obj o, const char* k, uint16_t def) {
        if (!o || o[k].isNull()) return def;
        long v = o[k].template as<long>();
        return (uint16_t)(v < 0 ? 0 : (v > 65535 ? 65535 : v));
    }
    uint32_t base_wall_ms_ = 0;
};

// Единственный экземпляр объявляет прошивка (см. firmware/ct_native): CtIr ctIr;

// ----------------------------------------------------------------------------
// ИНТЕГРАЦИЯ (минимальный дифф в чужие прошивки — ровно 4 места)
//
// 1) В начало скетча:      #include "ct_ir.h"   и   CtIr ctIr;
// 2) setup():              ctIr.begin(); подписка MQTT на ctIr.cfg.* топики
//                          -> колбэк: ctIr.applyJson(payload, len);
// 3) loop():               ctIr.render(leds, NUM_LEDS); вместо прямой записи
//                          (или перед Show() — родная логика не трогается);
// 4) Рендер-хук:           ctIr.onRender(myRenderHook); myRenderHook получает
//                          CtSnapshot (класс/параметры/палитру/сегмент/телемет-
//                          рию/фазу) и пишет пиксели. Неизвестный класс ->
//                          CtClass::Custom: ваш код решает (деградация/стоп),
//                          молчаливой подмены нет по контракту прослойки.
//
// Для WLED-форка дополнительно: /json/state принимает те же IR-JSON через
// POST /ct/ir (один обработчик -> ctIr.applyJson), сегментация ложится на
// нативные seg[i].start/stop. Для GyverLamp2/GyverHub — то же самое поверх
// их родного транспорта; всё остальное в прошивке остаётся нетронутым.
// ----------------------------------------------------------------------------

#endif  // CT_IR_H
