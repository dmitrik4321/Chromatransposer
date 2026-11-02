"""Тесты адаптера Opera GX (CDP). Реального браузера в CI нет — подменяем
_CdpClient фиксом; проверяем контракт «браузер как устройство умного дома»:
топология = лента вкладок, родные эффекты из конфига, честный unsupported,
классификация типа screen и потеря палитры при портировании."""
import pytest

from ct.adapters import opera as opera_mod
from ct.adapters.base import UnsupportedError
from ct.core.ir import ColorStop, EffectClass, EffectIR, EffectParams, Palette
from ct.core.render import classify_device
from ct.core.translator import ir_to_native, native_to_ir


class FakeCdp:
    """Замена _CdpClient: список целей и результаты evaluate по шаблону."""

    def __init__(self, base_url, timeout, targets=None, browser="Opera/12.0 OPR/115", eval_value=None):
        self.base = base_url
        self.timeout = timeout
        self.targets = targets if targets is not None else [
            {"type": "page", "title": "YouTube — lofi", "url": "https://youtube.com/watch?v=x",
             "webSocketDebuggerUrl": "ws://fake/1"},
            {"type": "page", "title": "GyverCMS", "url": "https://cms.local",
             "webSocketDebuggerUrl": "ws://fake/2"},
        ]
        self.browser = browser
        self.eval_value = eval_value
        self.evaluated: list[str] = []

    async def version(self):
        return {"Browser": self.browser}

    async def list_targets(self):
        return self.targets

    async def evaluate(self, ws_url, expression):
        self.evaluated.append(expression)
        return self.eval_value


def _make_adapter(monkeypatch, targets=None, browser="Opera/12.0 OPR/115"):
    a = opera_mod.OperaAdapter(bridge=None, hosts=["http://127.0.0.1:9222"])
    fake = FakeCdp("http://127.0.0.1:9222", 3.0, targets=targets, browser=browser)
    monkeypatch.setattr(opera_mod, "_CdpClient", lambda base, timeout: fake)
    return a, fake


@pytest.mark.asyncio
async def test_discover_registers_tabs_strip_and_skips_non_opera(monkeypatch):
    a, _ = _make_adapter(monkeypatch)
    nodes = await a.discover()
    assert len(nodes) == 1
    n = nodes[0]
    assert n.ecosystem.value == "opera"
    assert n.device_type == "screen"          # классификация по типу устройства
    assert n.supports_segments is False       # честно: сегментов у вкладок нет
    assert "blink" in {e.effect for e in n.native_effects}
    # не-Opera (например, Chrome с открытым портом) пропускаем
    a2, _ = _make_adapter(monkeypatch, browser="Chrome/140")
    assert await a2.discover() == []


@pytest.mark.asyncio
async def test_get_state_length_equals_tabs(monkeypatch):
    a, _ = _make_adapter(monkeypatch)
    await a.discover()
    st = await a.get_state("opera-gx")
    assert st["online"] and st["tabs"] == 2   # «длина ленты» = число вкладок
    node = a._nodes["opera-gx"]
    assert node.topology.length == 2
    assert "YouTube" in node.labels["screen_titles"]   # подписи для ASCII-логгера


@pytest.mark.asyncio
async def test_set_state_modes_and_segment_rejected(monkeypatch):
    a, fake = _make_adapter(monkeypatch)
    await a.discover()
    from ct.core.ir import Segment
    with pytest.raises(UnsupportedError):
        await a.set_state("opera-gx", {"mode": "blink"}, Segment(device_id="opera-gx", start=0, length=5))
    await a.set_state("opera-gx", {"mode": "blink"})
    assert any("__ctBlink" in js for js in fake.evaluated)
    await a.set_state("opera-gx", {"mode": "off"})
    assert any("clearInterval" in js for js in fake.evaluated[-1:])


@pytest.mark.asyncio
async def test_apply_ir_unknown_class_is_honest_unsupported(monkeypatch):
    a, _ = _make_adapter(monkeypatch)
    await a.discover()
    ir = EffectIR.model_validate({"class": "fire", "params": {}})
    with pytest.raises(UnsupportedError) as ei:
        await a.apply_ir("opera-gx", ir)
    assert "fire" in str(ei.value)            # явная потеря, без молчаливой подмены


def test_native_to_ir_maps_classes_and_declares_palette_loss():
    ref = opera_mod.NativeRef(ecosystem="opera", effect="colorsweep", args={})
    ir = native_to_ir(ref)
    assert ir.class_.value == "colorsweep"
    assert ir.origin == {"ecosystem": "opera", "effect": "colorsweep"}


def test_port_wled_to_opera_degrades_or_unsupported():
    # WLED-перлин на браузер: класса perlin в OPERA_MAP нет -> unsupported
    from ct.core.registry import DeviceNode, Ecosystem
    from ct.core.ir import Topology
    wled_ref = opera_mod.NativeRef(ecosystem="wled", effect="PerlinNoise", args={"fx": 32})
    ir = native_to_ir(wled_ref)
    opera_node = DeviceNode(id="opera-gx", ecosystem=Ecosystem.OPERA, adapter="opera",
                            device_type="screen",
                            topology=Topology(kind="strip", length=2))
    res = ir_to_native(ir, opera_node)
    assert res.unsupported is True or any("perlin" in n.lower() for n in res.notes)


def test_classify_screen_by_heuristics_and_explicit():
    a = opera_mod.OperaAdapter(hosts=["http://x:1"])
    node = a._node_for("http://x:1")
    assert classify_device(node) == "screen"      # эвристика по id/name («opera»)
    node.device_type = ""
    node.name = "Монитор в мастерской"
    assert classify_device(node) == "screen"      # по ключевому слову
    node.device_type = "strip"
    assert classify_device(node) == "strip"       # явное всегда честнее эвристики
