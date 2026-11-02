"""Тесты коннектора к официальному Opera smart-home расширению (opera_mqtt).

github.com/operasoftware/opera-smart-home: расширение само публикует состояние
в брокер; мы только читаем opera/<prefix>/..., строим канонический узел,
пишем команды ON/OFF сырым payload (base/switch.js ждёт ровно 'ON'/'OFF')
и применяем политику Panic Button без правки исходников Opera.
"""
import asyncio

import pytest

from ct.adapters.opera_mqtt import OperaMqttAdapter
from ct.core.ha_discovery import HaDiscoveryExporter
from ct.core.ir import EffectIR, EffectClass
from ct.adapters.base import UnsupportedError


class FakeBridge:
    """Миниatura MqttBridge: записи подписок/публикаций для проверок."""

    def __init__(self):
        self.subs = []
        self.published = []      # (topic, payload, retain)
        self.raw = []            # (topic, text) — publish_raw
        self.lwt = []

    def on(self, pattern, handler):
        self.subs.append((pattern, handler))

    async def publish(self, topic, payload, retain=False, qos=1):
        self.published.append((topic, payload, retain))

    async def publish_raw(self, topic, text, retain=False, qos=1):
        self.raw.append((topic, text))

    async def publish_lwt(self, topic):
        self.lwt.append(topic)

    @staticmethod
    def t_state(dev): return f"ct/v1/{dev}/state"
    @staticmethod
    def t_avail(dev): return f"ct/v1/{dev}/available"
    @staticmethod
    def t_set(dev): return f"ct/v1/{dev}/set"
    @staticmethod
    def t_telemetry(dev, kind): return f"ct/v1/{dev}/telemetry/{kind}"


@pytest.fixture
def adapter():
    br = FakeBridge()
    a = OperaMqttAdapter(bridge=br)
    yield a, br
    # teardown в том же цикле, где создавались задачи (иначе cancel() на
    # закрытом дефолтном лупе падает с "Event loop is closed")
    _LOOP.run_until_complete(a.close())


_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def test_discover_subscribes_opera_space(adapter):
    a, br = adapter
    run(a.discover())
    assert ("opera/#", a._on_opera_msg) in br.subs


def test_lazy_node_from_traffic_and_state(adapter):
    a, br = adapter
    run(a.discover())
    # официальное расширение шлёт состояния по своим топикам
    run(a._on_opera_msg("opera/gx/sensor/number_of_tabs", {"raw": "4"}))
    run(a._on_opera_msg("opera/gx/sensor/accent_color", {"raw": "#ff0055"}))
    node = a._nodes["gx"]
    assert node.id == "opera-gx-mqtt-gx"
    st = run(a.get_state(node.id))
    assert st["on"] is True and st["tabs"] == 4
    assert st["color"] == {"r": 255, "g": 0, "b": 85}
    # canonical state опубликован с retain + availability online
    topics = [t for t, _, _ in br.published]
    assert f"ct/v1/{node.id}/state" in topics
    assert f"ct/v1/{node.id}/available" in topics


def test_switches_ingested(adapter):
    a, br = adapter
    run(a.discover())
    run(a._on_opera_msg("opera/gx/switch/mute_all_tabs", {"raw": "ON"}))
    st = run(a.get_state("opera-gx-mqtt-gx"))
    assert st["switch_mute_all_tabs"] is True


def test_panic_on_gets_auto_off_raw_payload(adapter):
    """Хотелка: panic_mode включили -> прослойка гасит OFF штатной командой,
    payload СЫРОЙ 'OFF' (расширение сравнивает строку напрямую)."""
    a, br = adapter
    run(a.discover())
    run(a._on_opera_msg("opera/gx/sensor/number_of_tabs", {"raw": "1"}))
    run(a._on_opera_msg("opera/gx/switch/panic_mode", {"raw": "ON"}))
    assert ("opera/gx/switch/panic_mode/set", "OFF") in br.raw
    # debounce: второй ON подряд не долбит команду
    n_before = len(br.raw)
    run(a._on_opera_msg("opera/gx/switch/panic_mode", {"raw": "ON"}))
    assert len(br.raw) == n_before


def test_set_state_commands_use_raw_on_off(adapter):
    a, br = adapter
    run(a.discover())
    run(a._on_opera_msg("opera/gx/sensor/number_of_tabs", {"raw": "2"}))
    run(a.set_state("opera-gx-mqtt-gx", {"mode": "mute"}))
    assert ("opera/gx/switch/mute_all_tabs/set", "ON") in br.raw
    run(a.set_state("opera-gx-mqtt-gx", {"mode": "off"}))
    assert ("opera/gx/switch/mute_all_tabs/set", "OFF") in br.raw[-1:]


def test_colorsweep_honest_unsupported(adapter):
    a, br = adapter
    run(a.discover())
    run(a._on_opera_msg("opera/gx/sensor/number_of_tabs", {"raw": "2"}))
    with pytest.raises(UnsupportedError):
        run(a.set_state("opera-gx-mqtt-gx", {"mode": "colorsweep"}))


def test_segments_unsupported(adapter):
    from ct.core.ir import Segment
    a, br = adapter
    run(a.discover())
    run(a._on_opera_msg("opera/gx/sensor/number_of_tabs", {"raw": "2"}))
    with pytest.raises(UnsupportedError):
        run(a.set_state("opera-gx-mqtt-gx", {"on": True},
                        segment=Segment(device_id="opera-gx-mqtt-gx", start=0, length=2)))


def test_ir_perlin_unsupported_blink_ok(adapter):
    a, br = adapter
    run(a.discover())
    run(a._on_opera_msg("opera/gx/sensor/number_of_tabs", {"raw": "2"}))
    with pytest.raises(UnsupportedError):
        run(a.apply_ir("opera-gx-mqtt-gx",
                       EffectIR(**{"class": EffectClass.PERLIN})))
    res = run(a.apply_ir("opera-gx-mqtt-gx",
                         EffectIR(**{"class": EffectClass.BLINK})))
    assert res["mode"] == "blink"


# ---------------------- политика HA (Panic Button из UI убрать) ---------------

class FakeRegistry:
    def all(self):
        return []


def test_ha_policy_hides_panic(adapter):
    a, _ = adapter
    pol = a.ha_entity_policy()
    assert "switch.panic_mode" in pol["hidden"]
    assert pol["auto_off"]["switch.panic_mode"] is True


def test_discovery_effect_list_excludes_panic(adapter):
    a, br = adapter
    run(a.discover())
    run(a._on_opera_msg("opera/gx/sensor/number_of_tabs", {"raw": "2"}))
    node = a._nodes["gx"]
    exp = HaDiscoveryExporter(br, FakeRegistry(), adapters={a.name: a})
    run(exp.publish_device(node))
    cfg = [p for t, p, r in br.published if t.endswith("light/config")][-1]
    assert "panic_mode" not in cfg["effect_list"]
    assert "blink" in cfg["effect_list"]


def test_discovery_no_adapters_still_works(adapter):
    """Регресс: экспортёр без карты адаптеров не падает (политик нет)."""
    a, br = adapter
    run(a.discover())
    run(a._on_opera_msg("opera/gx/sensor/number_of_tabs", {"raw": "2"}))
    node = a._nodes["gx"]
    exp = HaDiscoveryExporter(br, FakeRegistry())
    run(exp.publish_device(node))
    cfgs = [(t, p) for t, p, r in br.published if t.endswith("light/config")]
    assert cfgs, f"discovery-конфиг не опубликован; topics={[t for t,_,_ in br.published]}"
    cfg = cfgs[-1][1]
    assert "panic_mode" in cfg["effect_list"]   # без политики — как есть
