// ============================================================================
// ct_native.ino — пример native_ct прошивки: лента + Wi-Fi + MQTT + IR-хук.
//
// СТИЛЬ (по договорённости): никакого хардкода в теле скетча — всё, что может
// потребоваться изменить, лежит в КОНФИГе ниже и в CtIrConfig.h (SSID/брокер).
// Исходники экосистем этот файл не правит: это своя прошивка, data plane
// (рендер) остаётся здесь, прослойка лишь диктует ЧТО рисовать через EffectIR.
//
// Что принимает по WiFi (контракт firmware/README.md):
//   1. JSON-инструкцию эффекта (EffectIR)          -> ctIr.applyJson()
//   2. сегментацию виртуальных пикселей            -> segment{start,length}
//   3. телеметрию синхронных узлов (звук и т.п.)   -> sensor{kind,value}
//   4. sync tick (общая фаза стенда, решение B)    -> sync{tick_seq,wall_ts}
// ============================================================================

#include <WiFi.h>
#include <PubSubClient.h>
#include <FastLED.h>
#include "ct_ir.h"          // интерпретатор прослойки (один include)
#include "CtIrConfig.h"     // provisioning-конфиг: SSID/пароль/брокер из CLI

// ------------------------- КОНФИГ ПЛАТФОРМЫ ---------------------------------
// Единственное место, где меняются параметры узла. Топики/watchdog/drift —
// в CtIrConfig (верх ct_ir.h), правятся сервером на лету без перепрошивки.
#ifndef CT_DEVICE_ID
#define CT_DEVICE_ID "native-01"       // выдаёт `ct provision` (решение E)
#endif
#ifndef CT_NUM_LEDS
#define CT_NUM_LEDS 144                // физическая лента этого узла
#endif
#ifndef CT_DATA_PIN
#define CT_DATA_PIN 2                  // GPIO ленты
#endif
#ifndef CT_LED_TYPE
#define CT_LED_TYPE WS2812B            // тип шины для FastLED
#endif
#ifndef CT_COLOR_ORDER
#define CT_COLOR_ORDER GRB             // порядок цветов этой сборки
#endif
// fallback-сцена (watchdog: сервер молчит > cfg.watchdog_ms):
#ifndef CT_FALLBACK_R
#define CT_FALLBACK_R 24               // тусклый янтарь = «жив, но без команд»
#endif
#ifndef CT_FALLBACK_G
#define CT_FALLBACK_G 8
#endif
#ifndef CT_FALLBACK_B
#define CT_FALLBACK_B 0
#endif
// калибр скорости фазы в хуке рендера (мс на цикл узора):
#ifndef CT_EFFECT_GAIN_MS
#define CT_EFFECT_GAIN_MS 400
#endif

CtIr ctIr;                              // единственный экземпляр (extern в ct_ir.h)
static TPixel leds[CT_NUM_LEDS];
WiFiClient espClient;
PubSubClient mqtt(espClient);

// ---------------- ХУК РЕНДЕРА (data plane остаётся в прошивке) --------------
// Прослойка дала класс/параметры/палитру/сегмент/телеметрию — КАК рисовать
// решает прошивка. Неизвестный класс приходит как CtClass::Custom: рисуем
// выключенные пиксели (честный unsupported, молчаливой подмены нет).
void ctRender(const CtIr::CtSnapshot& s, TPixel* out, uint16_t n) {
    for (uint16_t i = 0; i < n; i++) {
        // виртуальная координата: этот контроллер закрывает холст
        // [segment.start .. segment.start+n); другой частью рулят другие узлы.
        uint32_t vx = s.segment.start + i;
        uint16_t u;                                   // параметр цвета 0..1000
        switch (s.cls) {
            case CtClass::Off:    out[i] = TPixel{0, 0, 0}; continue;
            case CtClass::Static: u = 0; break;
            case CtClass::Rainbow:                   // бегущая радуга по холсту
                u = (vx * 1000 / max(1u, n) +
                     (uint32_t)s.phase_ms * s.params.speed / 500) % 1000;
                break;
            case CtClass::Sweep:                     // волна яркости
                u = (vx * 4 + s.phase_ms / 8) % 1000;
                break;
            case CtClass::Custom:                    // неизвестный класс — стоп
                out[i] = TPixel{0, 0, 0}; continue;
            default:                                 // perlin/fire/rain — сюда
                u = ((uint32_t)s.phase_ms * s.params.speed / CT_EFFECT_GAIN_MS +
                     vx * (s.params.scale + 1) / 32) % 1000;
                break;
        }
        TPixel c = ctIr.paletteAt(u);
        // brightness из IR + телеметрия звука от синхронного узла с микрофоном
        float k = s.params.brightness / 1000.0f;
        for (uint8_t j = 0; j < s.sensor_count; j++) {
            if (!strcmp(s.sensors[j].kind, "audio_level"))
                k *= 0.5f + min(1.0f, s.sensors[j].value);   // «дышит» музыкой
        }
        out[i].r = c.r * k; out[i].g = c.g * k; out[i].b = c.b * k;
    }
}

void ctFallback(TPixel* out, uint16_t n) {
    for (uint16_t i = 0; i < n; i++) {
        out[i].r = CT_FALLBACK_R; out[i].g = CT_FALLBACK_G; out[i].b = CT_FALLBACK_B;
    }
}

// ------------------------- MQTT glue ----------------------------------------
void onMqtt(char*, byte* payload, unsigned int len) {
    ctIr.applyJson((const char*)payload, len);        // одна точка входа
}

String ctTopic(const char* suffix) {
    char t[96];
    snprintf(t, sizeof t, "%s/%s%s", ctIr.cfg.topic_prefix, CT_DEVICE_ID, suffix);
    return String(t);
}

void publishState() {
    StaticJsonDocument<256> d;
    auto snap = ctIr.snapshot();
    d["state"] = "online";
    d["cls"]   = ctClassName(snap.cls);
    d["phase"] = snap.phase_ms;
    d["fallback"] = snap.in_fallback;
    char buf[256];
    size_t n = serializeJson(d, buf);
    mqtt.publish(ctTopic(ctIr.cfg.topic_suffix_state).c_str(),
                 (const uint8_t*)buf, n, true);       // retain (решение D)
}

void setup() {
    ctIr.begin();
    ctIr.onRender(ctRender);
    ctIr.onFallback(ctFallback);

    FastLED.addLeds<CT_LED_TYPE, CT_DATA_PIN, CT_COLOR_ORDER>(leds, CT_NUM_LEDS);

    WiFi.mode(WIFI_STA);
    WiFi.begin(CtCfg::ssid(), CtCfg::password());
    mqtt.setServer(CtCfg::brokerHost(), CtCfg::brokerPort());
    while (!mqtt.connected()) {
        // LWT сообщает брокеру наш offline — так реестр видит обрыв связи
        if (mqtt.connect(CT_DEVICE_ID,
                         ctTopic(ctIr.cfg.topic_suffix_available).c_str(),
                         1, true, "{\"state\":\"offline\"}")) break;
        delay(1000);
    }
    mqtt.subscribe(ctTopic(ctIr.cfg.topic_suffix_ir).c_str());
    mqtt.subscribe(ctTopic(ctIr.cfg.topic_suffix_set).c_str());
    mqtt.subscribe(ctIr.cfg.topic_sync_tick);         // общий тик стенда
    mqtt.setCallback(onMqtt);
}

uint32_t last_pub = 0;
void loop() {
    mqtt.loop();
    ctIr.render(leds, CT_NUM_LEDS);                   // IR -> хук прошивки
    FastLED.show();                                   // effects считает здесь
    if (millis() - last_pub > CtCfg::state_publish_ms()) {
        last_pub = millis();
        publishState();
    }
}
