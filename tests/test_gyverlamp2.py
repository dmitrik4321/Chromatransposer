"""Тесты протокола GyverLamp2 и адаптера — по данным сообщества
(smeleshkin/GyverLamp2HA, docs/protocol.md). Порт 61197 для GL/1 — из их же доков."""
import pytest

from ct.adapters import gyverlamp2_protocol as gl
from ct.core.ir import ColorStop, EffectClass, EffectIR, EffectParams, NativeRef, Palette
from ct.core.registry import DeviceNode, Ecosystem, OnlineState, TransportKind
from ct.core.translator import ir_to_native, native_to_ir


def test_udp_port_gl_group1_is_61197():
    # эталонное значение из docs/protocol.md GyverLamp2HA: «Для GL/1 получается 61197»
    assert gl.udp_port(group=1, key="GL") == 61197


def test_heartbeat_parse():
    assert gl.parse_heartbeat(b"GL_ONL7") == 7
    assert gl.parse_heartbeat(b"GL_ONL39") == 39        # MAX_PRESETS=40: 0..39 валидны
    assert gl.parse_heartbeat(b"GL_ONL40") is None     # вне диапазона
    assert gl.parse_heartbeat(b"GL_ONL100") is None    # regex ловит только 1-2 цифры
    assert gl.parse_heartbeat(b"GL,0,1,0,1,2,3") is None


def test_control_and_slave_packets():
    from datetime import datetime
    now = datetime(2026, 11, 2, 12, 30, 45)  # понедельник -> d=(0+1)%7=1
    assert gl.control_packet(gl.ControlCommand.ON, now) == b"GL,0,1,1,12,30,45"
    assert gl.slave_brightness(200) == b"GL,4,2,200"
    assert gl.slave_preset(3) == b"GL,4,1,3"
    with pytest.raises(ValueError):
        gl.slave_brightness(0)   # 0 => выключение отдельной командой


def test_mode_bank_packet_structure():
    row = [0] * gl.MODE_FIELDS
    row[gl.ModeField.EFFECT] = int(gl.FirmwareEffect.FIRE)
    row[gl.ModeField.BRIGHTNESS] = 255
    row[gl.ModeField.ADV_MODE] = 1
    row[gl.ModeField.SOUND_REACT] = 1
    row[gl.ModeField.PALETTE] = gl.HEAT_PALETTE_ID
    pkt = gl.mode_bank_packet([tuple(row)], selected=0).decode()
    f = pkt.split(",")
    assert f[0] == "GL" and f[1] == "2" and f[2] == "1"     # префикс, тип, N
    assert int(f[3]) == 6                                    # effect=fire
    assert int(f[-5]) == 1                                   # slot сдвинут на +1
    assert len(f) == 3 + gl.MODE_FIELDS + 1 + 4              # +slot +d,h,m,s
    with pytest.raises(ValueError):
        gl.validate_mode_record(tuple([0] * gl.MODE_FIELDS))  # effect=0 невалиден


def test_palette_ids_match_firmware_order():
    assert gl.palette_id("heat") == 2
    assert gl.palette_id("rainbow") == 12
    assert gl.palette_id(32) == 32 and gl.palette_name(32) == "aurora"
    with pytest.raises(ValueError):
        gl.palette_id("nonexistent")


@pytest.fixture
def lamp_node():
    return DeviceNode(id="lamp-desk", ecosystem=Ecosystem.GYVERLAMP2,
                      adapter="gyverlamp2",
                      topology={"kind": "strip", "length": 60, "width": 1, "height": 1},
                      endpoints=[],
                      native_effects=[NativeRef(ecosystem="gyverlamp2", effect=k)
                                      for k in gl.EFFECT_KEYS.values()])


def test_native_to_ir_gyverlamp2_byte_scales():
    ir = native_to_ir(NativeRef(ecosystem="gyverlamp2", effect="fire"),
                      {"speed": 255, "scale": 0, "bright": 128, "palette": 2})
    assert ir.class_ is EffectClass.FIRE
    assert ir.params.speed == 1.0 and ir.params.scale == 0.0
    assert abs(ir.params.brightness - 128 / 255) < 1e-6
    assert ir.palette.name == "heat"          # id 2 -> имя; стоп нет — потеря градиента


def test_ir_to_lamp_fire_direct_with_bytes(lamp_node):
    ir = EffectIR(**{"class": EffectClass.FIRE},
                  params=EffectParams(speed=0.5, brightness=1.0),
                  palette=Palette(name="heat"))
    res = ir_to_native(ir, lamp_node)
    assert not res.unsupported
    assert res.target_native.effect == "fire"
    assert res.target_native.args["speed"] == 128
    assert res.target_native.args["palette"] == 2


def test_ir_to_lamp_plasma_degrades_not_silent(lamp_node):
    """У лампы нет plasma — только явная деградация или честный unsupported."""
    ir = EffectIR(**{"class": EffectClass.PLASMA}, palette=Palette(name="ocean"))
    res = ir_to_native(ir, lamp_node)
    assert res.degraded_to is not None or res.unsupported
    if res.target_native:
        assert res.target_native.effect in gl.EFFECT_KEYS.values()
        assert any("ocean" in n or "degrad" in n.lower() for n in res.notes + (res.degraded_to.losses if res.degraded_to else []))


def test_lamp_to_wled_roundtrip_keeps_class():
    wled = DeviceNode(id="wled-1", ecosystem=Ecosystem.WLED, adapter="wled",
                      topology={"kind": "matrix", "width": 16, "height": 16, "length": 256},
                      native_effects=[NativeRef(ecosystem="wled", effect="Fire2012")])
    ir = native_to_ir(NativeRef(ecosystem="gyverlamp2", effect="perlin"), {"speed": 100})
    res = ir_to_native(ir, wled)
    # в реестре wled-1 заявлен только Fire2012 -> perlin деградирует явно, не молча
    assert res.degraded_to is not None or res.target_native.effect.lower() in ("perlinnoise", "fire2012")
    if res.target_native.effect.lower() == "perlinnoise":
        assert res.target_native.args["sx"] == 100


@pytest.mark.asyncio
async def test_adapter_declares_no_segments(lamp_node):
    from ct.adapters.gyverlamp2 import GyverLamp2Adapter
    from ct.adapters.base import UnsupportedError
    from ct.core.ir import Segment
    ad = GyverLamp2Adapter(lamps={"lamp-desk": {"ip": "127.0.2.1"}}, listen=False)
    await ad.discover()
    with pytest.raises(UnsupportedError):
        await ad.set_state("lamp-desk", {"on": True}, segment=Segment(
            device_id="lamp-desk", start=0, length=30))
