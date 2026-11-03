# Прошивки ESP (устройства) — остаются data plane: эффекты считает прошивка.

Прослойка Chromatransposer — control plane на ПК. Каждая конечная прошивка
должна уметь принимать по WiFi:

1. **JSON-инструкцию эффекта (EffectIR)** — каноническая схема
   `{class, params{speed,scale,density,blur,brightness}, palette{stops}}`.
2. **Сегментацию**: `segment {start, length, canvas_offset}` — с какого
   виртуального пикселя начинается лента этого контроллера (другой частью
   рулят другие узлы).
3. **Телеметрию** от синхронизированных устройств: `sensor {kind, value, meta}`
   (например audio_level/audio_band с узла, у которого есть микрофон).
4. **Sync tick**: `sync {tick_seq, wall_ts, period_ms}` — общая фаза анимации.

## Транспорт

### native_ct (своя прошивка, MQTT + опц. HTTP)
Подписки:
```
ct/v1/<device_id>/ir        -> EffectIR JSON (+ segment)
ct/v1/<device_id>/set       -> state JSON / sensor / sync
ct/v1/sync/tick             -> общий тик (если нет точечной рассылки)
Публикации:
ct/v1/<device_id>/state          (retain)
ct/v1/<device_id>/available      (LWT: {"state":"offline"})
ct/v1/<device_id>/telemetry/<kind>  (звук и т.п.)
```

### WLED
Родной API не меняется: сервер транслирует IR -> нативные сегменты/эффекты
и шлёт через `/json/state` (HTTP) или WS `/ws`. Для «виртуальных пикселей»
используются сегменты WLED (`seg[i].start/stop`). Если форкаете WLED —
можно добавить обработчик топика `ct/v1/<id>/ir` как в native_ct.

### GyverHub-семейство
Маппинг схемных топиков `<base>/set|state|available|config` делает адаптер.
Стоковые прошивки не умеют сегментацию/IR — для честной работы стенда
рекомендуется форк с модулем IR-интерпретатора (см. contract выше); иначе
сервер декларирует потери при портировании.

## Схема provisioning (решение E)
ESP-прошивка при первом USB-подключении получает от CLI-программы
SSID/пароль и `shared_key`; далее подключается к локальному брокеру.
Пока окно раздачи закрыто, неизвестный device_id отвергается брокером/сервером.

## Требования к интерпретатору в прошивке (native_ct)
- Парсер JSON <= 2 КБ (ArduinoJson), идемпотентность: повтор IR = сброс фазы
  только если пришёл новый `tick_seq` baseline.
- Рендер эффектов — родной код прошивки; прослойка никогда не шлёт пиксельные буферы
  (DDP/Art-Net вне MVP, см. README).
- Watchdog: нет команд > N сек → переход в fallback-сцену из flash.

## Хостовые smoke-тесты ct_ir.h (без железа)
Переносимая логика заголовка (парсинг IR-JSON, палитра, sync tick, watchdog-
fallback) проверяется на ПК: `pytest tests/test_firmware_host.py`. Сборка идёт
g++ с заглушками `tests/host_stubs/` (Arduino.h, ArduinoJson.h — минимальный
API, ровно то, что трогает ct_ir.h) и `-DCT_HOST_TEST -DCT_NO_FASTLED`.
Тестовая программа: `tests/firmware_smoke.cpp`. Это НЕ эмуляция ESP: для
реальных плат по-прежнему нужен PlatformIO (native_ct) или форки WLED/Gyver.
