// ============================================================================
// CtIrConfig.h — локальный provisioning-конфиг native_ct прошивки.
//
// ЭТО ЕДИНСТВЕННЫЙ ФАЙЛ, который CLI-программа (`ct provision`) правит под
// конкретную сеть и узел. Скетч ct_native.ino отсюда берёт SSID/пароль/брокер;
// значения по умолчанию подходят для разработки (решение E: Wi-Fi данные
// выдаются через USB один раз, пока окно раздачи открыто).
//
// НИКАКОГО ДРУГОГО хардкода в скетче быть не должно — всё меняемое тут.
// ============================================================================

#ifndef CT_IR_CONFIG_H
#define CT_IR_CONFIG_H

#include <stddef.h>

namespace CtCfg {

// ---- Wi-Fi (заполняет `ct provision` при раздаче по USB) ---------------------
inline const char* ssid()      { return "CHANGE_ME_SSID"; }
inline const char* password()  { return "CHANGE_ME_PASS"; }

// ---- Локальный MQTT-брокер (решение D: только локальный сервер) --------------
inline const char* brokerHost() { return "192.168.1.10"; }  // ПК/RPi с mosquitto
inline uint16_t    brokerPort() { return 1883; }

// ---- Периоды ------------------------------------------------------------------
inline uint32_t state_publish_ms()   { return 5000; }  // как часто шлём /state
inline uint32_t mqtt_keepalive_s()   { return 15; }    // LWT-детекция офлайна

}  // namespace CtCfg

#endif
