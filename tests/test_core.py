"""Тесты ядра: IR-трансляция, арбитраж, реконсиляция, оркестрация."""
from __future__ import annotations

import asyncio
import time

import pytest

from ct.core.ir import EffectClass, EffectIR, EffectParams, NativeRef, Palette, ColorStop, Segment, Topology
from ct.core.orchestrator import Caller, ConflictError, Group, Orchestrator
from ct.core.registry import DeviceNode, Ecosystem, OnlineState, Registry
from ct.core.translator import ir_to_native, native_to_ir, port


def wled_node():
    return DeviceNode(id="wled-1", ecosystem=Ecosystem.WLED, adapter="wled",
                      topology=Topology(kind="strip", length=100))


def gyver_matrix():
    from ct.core.ir import NativeRef as NR
    return DeviceNode(id="matrix-1", ecosystem=Ecosystem.GYVERHUB, adapter="gyverhub",
                      topology=Topology(kind="matrix", width=16, height=16, length=256),
                      native_effects=[NR(ecosystem="gyverhub", effect=e)
                                      for e in ("noise", "fire", "solid")])


def native_node():
    return DeviceNode(id="strip-ct", ecosystem=Ecosystem.NATIVE_CT, adapter="native_ct",
                      topology=Topology(kind="strip", length=120),
                      supports_ir=True, supports_segments=True)


# --- IR core -------------------------------------------------------------------

def test_wled_native_to_ir_normalizes_scales():
    ref = NativeRef(ecosystem="wled", effect="PerlinNoise",
                    args={"sx": 255, "ix": 0, "bri": 128})
    ir = native_to_ir(ref)
    assert ir.class_ is EffectClass.PERLIN
    assert ir.params.speed == 1.0 and ir.params.density == 0.0
    assert abs(ir.params.brightness - 0.5) < 0.01


def test_roundtrip_wled_gyver_denorm():
    ref = NativeRef(ecosystem="wled", effect="Rain", args={"sx": 128})
    res = port(ref, gyver_matrix())
    assert not res.unsupported
    # 128/255 -> ~0.5 -> Gyver-шкала 0..100 -> ~50
    assert 45 <= res.target_native.args["speed"] <= 55


def test_topology_bound_degrades_with_declaration():
    """Fire (матричный) на ленту WLED — только с декларированными потерями."""
    ir = EffectIR(**{"class": EffectClass.FIRE})
    res = ir_to_native(ir, wled_node())
    assert res.degraded_to is not None or res.unsupported
    if res.degraded_to:
        assert any("composition" in l for l in res.degraded_to.losses)


def test_honest_unsupported_without_primitives():
    node = DeviceNode(id="dumb", ecosystem=Ecosystem.GENERIC, adapter="generic",
                      topology=Topology())
    ir = EffectIR(**{"class": EffectClass.PLASMA})
    res = ir_to_native(ir, node)
    assert res.unsupported is True   # ни одного класса — честный отказ


def test_palette_conversion():
    ref = NativeRef(ecosystem="wled", effect="Rainbow",
                    args={"palette": ["#ff0000", "#00ff00", "#0000ff"]})
    ir = native_to_ir(ref)
    assert [s.color for s in ir.palette.stops] == ["#ff0000", "#00ff00", "#0000ff"]
    out = ir_to_native(ir, gyver_matrix())
    assert out.target_native.args["pal"] == ["#ff0000", "#00ff00", "#0000ff"]


def test_direct_ir_for_native_firmware():
    ir = EffectIR(**{"class": EffectClass.PERLIN},
                  params=EffectParams(speed=0.7))
    res = ir_to_native(ir, native_node())
    assert res.target_native.effect == "perlin"
    assert res.notes == ["direct IR"]


# --- арбитраж: LWW + админ-приоритет ------------------------------------------------

@pytest.mark.asyncio
async def test_admin_capture_blocks_user_until_release():
    calls = []

    async def apply_fn(dev, state, seg, ir):
        calls.append((dev, state))
        return {"ok": dev}

    reg = Registry()
    reg.upsert(native_node())
    orch = Orchestrator(reg, apply_fn)
    admin, user = Caller(id="a", role="admin"), Caller(id="u", role="user")

    await orch.command_device("strip-ct", {"on": True}, admin)
    with pytest.raises(ConflictError):
        await orch.command_device("strip-ct", {"on": False}, user)
    # админ всегда может перезаписать себя
    await orch.command_device("strip-ct", {"on": False}, admin)
    orch.release_capture("strip-ct", admin)
    await orch.command_device("strip-ct", {"on": True}, user)  # теперь можно
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_capture_expires():
    async def apply_fn(dev, state, seg, ir):
        return {}
    reg = Registry(); reg.upsert(native_node())
    orch = Orchestrator(reg, apply_fn)
    await orch.command_device("strip-ct", {"on": True}, Caller(id="a", role="admin"))
    cap = orch._captures["strip-ct"]; cap.ts = time.time() - cap.ttl - 1
    await orch.command_device("strip-ct", {"on": False}, Caller(id="u", role="user"))


# --- реконсиляция и группы -----------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_fixes_drift():
    sent = []

    async def apply_fn(dev, state, seg, ir):
        sent.append(dict(state)); return {}
    reg = Registry(); reg.upsert(native_node())
    orch = Orchestrator(reg, apply_fn)
    await orch.command_device("strip-ct", {"on": True, "brightness": 90},
                              Caller(id="u", role="user"))
    orch.report_state("strip-ct", {"on": True, "brightness": 10})  # дрейф
    fixed = await orch.reconcile()
    assert "strip-ct" in fixed and fixed["strip-ct"] == ["brightness"]
    assert sent[-1]["brightness"] == 90


@pytest.mark.asyncio
async def test_group_single_command_many_nodes_with_segments():
    got = []

    async def apply_fn(dev, state, seg, ir):
        got.append((dev, seg.start if seg else None)); return {}
    reg = Registry()
    reg.upsert(DeviceNode(id="m-a", ecosystem=Ecosystem.NATIVE_CT, adapter="native_ct",
                         topology=Topology(), supports_ir=True))
    reg.upsert(DeviceNode(id="m-b", ecosystem=Ecosystem.NATIVE_CT, adapter="native_ct",
                         topology=Topology(), supports_ir=True))
    orch = Orchestrator(reg, apply_fn)
    orch.groups["wall"] = Group(id="wall", pattern=["m-*"], sync=True,
                                segments={"m-a": Segment(device_id="m-a", start=0, length=64, canvas_offset=0),
                                          "m-b": Segment(device_id="m-b", start=64, length=64, canvas_offset=64)})
    await orch.command_group("wall", {"on": True}, Caller(id="u", role="user"))
    assert sorted(got) == [("m-a", 0), ("m-b", 64)]


# --- расписания (backlog: планировщик был мёртвым кодом) -----------------------

@pytest.mark.asyncio
async def test_schedule_every_runs_scene():
    fired = []

    async def apply_fn(dev, state, seg, ir):
        return {}
    reg = Registry(); reg.upsert(native_node())
    orch = Orchestrator(reg, apply_fn)
    from ct.core.orchestrator import Scene, Schedule
    orch.scenes["s1"] = Scene(id="s1", steps=[{"device": "strip-ct", "state": {"on": True}}])
    orch.schedules["r1"] = Schedule(id="r1", scene_id="s1", cronish="every 5s")
    # однократный проход планировщика: первый тик должен запустить сцену
    await orch._scheduler_tick()
    assert "strip-ct" in orch._desired
    # повторный проход сразу — не должен запускать второй раз раньше срока
    n_before = len(orch._desired)
    await orch._scheduler_tick()
    assert len(orch._desired) == n_before


@pytest.mark.asyncio
async def test_schedule_at_fires_once_and_disabled_skipped():
    async def apply_fn(dev, state, seg, ir):
        return {}
    reg = Registry(); reg.upsert(native_node())
    orch = Orchestrator(reg, apply_fn)
    from ct.core.orchestrator import Scene, Schedule
    import datetime as dt
    past = (dt.datetime.now() - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    orch.scenes["s1"] = Scene(id="s1", steps=[{"device": "strip-ct", "state": {"on": True}}])
    orch.schedules["r-at"] = Schedule(id="r-at", scene_id="s1", cronish=f"at {past}")
    await orch._scheduler_tick()
    assert orch._desired.get("strip-ct") == {"on": True}
    orch._desired.clear()
    await orch._scheduler_tick()                    # однократно — больше не firing
    assert not orch._desired
    orch.schedules["r-at"].enabled = False          # disabled — пропускаем
    await orch._scheduler_tick()
    assert not orch._desired
