# Chromatransposer

Прослойка (control plane) над кучей LED-проектов, которая делает из них один
мега-проект. Эффекты по-прежнему считает прошивка устройств — прослойка даёт
**адресацию, трансляцию и оркестрацию**. Без облака: демон в LAN (ПК/RPi),
MQTT только локальный.

## Архитектура

```
Клиенты (CLI / HA / веб)                 Прошивки ESP (data plane)
        │                                        ▲
   Северный API (REST + WS + MQTT)          Южные адаптеры
   ct/north/rest_api.py                     ct/adapters/{wled,gyverhub,
        │                                   generic,native_ct}.py
        ▼                                        │
   ct/core: Registry ── Translator(IR) ── Orchestrator ── MqttBus(локальный брокер)
```

- **Effect IR** (`ct/core/ir.py`) — каноническая схема эффекта: класс
  (perlin/plasma/fire/rain/…), нормализованные параметры 0..1, палитра из
  упорядоченных стопов. Портирование = `native(source) → IR → native(target)`
  (`ct/core/translator.py`): нормализация шкал (WLED 0..255, Gyver 0..100),
  конвертер палитр, явные потери (деградация в композицию примитивов или
  честный `unsupported`, без молчаливой подмены). Топологически привязанные
  эффекты (матрица↔лента) портируются только с декларированными потерями.
- **Capability registry** (`ct/core/registry.py`) — канонический узел:
  топология 1D/2D, цветопорядок, гамма, FPS, родные эффекты, эндпоинты,
  online через LWT/retain.
- **Оркестрация** (`ct/core/orchestrator.py`) — группы/сцены/расписания,
  реконсиляция desired↔reported, общий sync-тик (drift <50 мс допустим),
  арбитраж конфликтов: **last-write-wins + приоритет администратора**
  (409 для не-админа до снятия захвата/TTL).
- **Виртуальные пиксели**: прошивка получает `segment {start,length,canvas_offset}`
  — знает, с какого пикселя общего холста начинается её лента; другой частью
  рулят другие контроллеры.
- **Телеметрия**: звук/свет с одного узла раздаётся синхронизированной группе
  (`ct/v1/<dev>/telemetry/<kind>` → `sensor{...}` в `set` остальным).
- **Аутентификация (решение E)**: устройство получает ID через USB+SSID/пароль
  в период открытого окна раздачи; закрытое окно = новых устройств нет. Клиентам
  API выдаются токены (user/admin) тем же механизмом окон. Устройств-токенов нет.
- **DDP/Art-Net/sACN** — вне MVP (решение C): control plane не шлёт пиксельные
  буферы; при необходимости добавится как отдельный «сырой» южный адаптер.

## Запуск

```bash
pip install -r requirements.txt
# 1) локальный брокер (например mosquitto) на :1883
# 2) сервер:
python main.py                       # REST :8300, конфиг configs/server.yaml
# 3) консольная программа (тот же ПК):
python -m ct.south_cli.cli --token <выданный_токен>
#    или всё в одном процессе (для USB-прожинга без сети):
python -m ct.south_cli.cli --local
```

Конфиг как код: `configs/server.yaml`, generic-маппинги `configs/generic/*.yaml`.
Состояние прожинга — `state/provisioning.json`.

## CLI (Rich REPL)

```
ct> devices                         таблица парка (экосистема/топология/IR/online)
ct> set wled-1 on=true bri=128      команда устройству
ct> ir strip-corridor @effect.json  EffectIR напрямую в native_ct-прошивку
ct> port {"ecosystem":"wled","effect":"Fire2012"} matrix-wall
                                    портирование с отчётом о потерях
ct> provision window devices on     открыть окно раздачи device_id
ct> provision usb esp42 home24 s3cret   выдать ID устройству по USB
ct> token issue phone user          выдать API-токен, затем закрыть окно
ct> reconcile                       правка дрейфа desired vs reported
ct> tick start 5                    общий тик стендов, 5 Гц
```

## Тесты

```bash
python -m pytest tests/ -q    # 18 passed: IR-трансляция, арбитраж, API, сегменты
```

## Структура

```
ct/core/       ir.py registry.py translator.py orchestrator.py mqtt_bus.py auth.py server.py
ct/adapters/   base.py wled.py gyverhub.py generic.py native_ct.py
               # плагины по интерфейсу discover/capabilities/get_state/set_state/subscribe
ct/north/      rest_api.py       # единый REST+WS для клиентов
ct/south_cli/  cli.py            # консольная программа на ПК
firmware/README.md               # контракт JSON-инструкций для прошивок ESP
configs/       server.yaml, generic/*.yaml
tests/         test_core.py test_api.py
```

## Home Assistant

HA не является зависимостью. При желании интеграция ложится поверх того же
северного API (MQTT-discovery-топики `ct/v1/*/state|set` уже близки к схеме
HA MQTT-discovery) либо community-плагином — это клиент, а не часть прослойки.
