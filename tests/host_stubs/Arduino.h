// ============================================================================
// Хостовая заглушка Arduino.h для smoke-тестов компиляции firmware/ct_ir.h.
// НЕ часть прошивки: только тестовая сборка (g++ -DCT_HOST_TEST).
// Минимальный объём: millis() + типы, которые реально трогает ct_ir.h.
// Виртуальные часы: тесты дёргают ct_test_advance(), чтобы двигать millis().
// ============================================================================
#ifndef CT_STUB_ARDUINO_H
#define CT_STUB_ARDUINO_H

#include <cstdint>
#include <cstdlib>

inline uint32_t& ct_millis_ref() { static uint32_t t = 0; return t; }
inline uint32_t millis() { return ct_millis_ref(); }
inline void delay(unsigned) {}
// Arduino-макросы, которых нет в std. Без include_next (нестабильно между
// версиями libstdc++): просто inline-шаблоны в глобальной области видимости —
// ct_ir.h вызывает min() с двумя аргументами, перегрузка из <algorithm>
// при этом не конфликтует (unqualified lookup найдёт обе, точное совпадение
// arity решает).
template <typename T, typename U> inline auto min(T a, U b) -> decltype(a < b ? a : b) { return a < b ? a : b; }
template <typename T, typename U> inline auto max(T a, U b) -> decltype(a > b ? a : b) { return a > b ? a : b; }
inline int abs(int v) { return v < 0 ? -v : v; }
inline long abs(long v) { return v < 0 ? -v : v; }

inline void ct_test_advance(uint32_t ms) { ct_millis_ref() += ms; }

#endif // CT_STUB_ARDUINO_H
