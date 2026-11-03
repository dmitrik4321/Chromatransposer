"""Оркестрация: группы, сцены, расписания, реконсиляция, арбитраж, sync-tick.

Арбитраж конфликтов (решение А): last-write-wins + приоритет администратора.
Не-админ не может перезаписать состояние, установленное админом, пока админ
не отпустит «захват» (или не истечёт capture_ttl). Попытка -> 409 Conflict.

Синхронизация (решение B): общий тик — сервер рассылает tick_seq+wall_ts всем
участникам стенда; прошивки выставляют фазу по часам, дрейф <50 мс допустим,
покадровый sync не гарантируется.
"""
from __future__ import annotations

import asyncio
import fnmatch
import time
from typing import Any, Optional

from pydantic import BaseModel, Field

from ct.core.ir import EffectIR, Segment
from ct.core.registry import DeviceNode, Registry


class Principal(str):
    """Идентификатор клиента API. role: 'user' | 'admin'."""


class Caller(BaseModel):
    id: str
    role: str = "user"          # user | admin
    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Capture(BaseModel):
    device_id: str
    caller_id: str
    role: str
    ts: float
    ttl: float = 3600.0

    @property
    def expired(self) -> bool:
        return time.time() - self.ts > self.ttl


class ConflictError(RuntimeError):
    def __init__(self, device_id: str, holder: Capture):
        super().__init__(
            f"device '{device_id}' captured by '{holder.caller_id}' (role={holder.role}); "
            "last-write-wins blocked for lower-priority caller")
        self.device_id = device_id
        self.holder = holder


class Group(BaseModel):
    id: str
    name: str = ""
    pattern: list[str] = Field(default_factory=list)   # glob по id/labels
    segments: dict[str, Segment] = Field(default_factory=dict)  # виртуальные пиксели
    canvas_length: int = 0
    sync: bool = False          # стенд с общим тиком


class Scene(BaseModel):
    id: str
    name: str = ""
    steps: list[dict] = Field(default_factory=list)    # [{device|group, state|ir}]
    fade_ms: int = 0


class Schedule(BaseModel):
    id: str
    scene_id: str
    cronish: str              # упрощённо: "daily HH:MM" | "every Ns" | "at ISO"
    enabled: bool = True


class Orchestrator:
    def __init__(self, registry: Registry, apply_fn):
        """apply_fn(device_id, state_dict, segment, ir) — единая точка выдачи
        команды на юг (реализует трансляцию IR->native внутри)."""
        self.registry = registry
        self.apply = apply_fn
        self.groups: dict[str, Group] = {}
        self.scenes: dict[str, Scene] = {}
        self.schedules: dict[str, Schedule] = {}
        self._captures: dict[str, Capture] = {}      # desired-state арбитраж
        self._desired: dict[str, dict[str, Any]] = {}  # реконсиляция: желаемое
        self._desired_ir: dict[str, EffectIR] = {}     # предпросмотр: IR для ASCII-логгера
        self._reported: dict[str, dict[str, Any]] = {} # фактическое
        self._tick_task: Optional[asyncio.Task] = None
        self._seq = 0

    # --- арбитраж -----------------------------------------------------------
    def check_capture(self, device_id: str, caller: Caller) -> None:
        cap = self._captures.get(device_id)
        if cap and not cap.expired and cap.role == "admin" and not caller.is_admin:
            raise ConflictError(device_id, cap)

    def record_capture(self, device_id: str, caller: Caller) -> None:
        if caller.is_admin:
            self._captures[device_id] = Capture(device_id=device_id,
                                                caller_id=caller.id, role="admin",
                                                ts=time.time())

    def release_capture(self, device_id: str, caller: Caller) -> None:
        cap = self._captures.get(device_id)
        if cap and (cap.caller_id == caller.id or caller.is_admin):
            self._captures.pop(device_id, None)

    # --- команды ------------------------------------------------------------
    async def command_device(self, device_id: str, state: dict, caller: Caller,
                             ir: Optional[EffectIR] = None,
                             segment: Optional[Segment] = None) -> dict:
        node = self.registry.get(device_id)
        if node is None:
            raise KeyError(f"unknown device {device_id}")
        self.check_capture(device_id, caller)
        seg = segment or self._segment_for(device_id)
        result = await self.apply(device_id, state, seg, ir)
        self._desired[device_id] = state
        if ir is not None:
            self._desired_ir[device_id] = ir   # ASCII-логгер рисует именно desired
        elif state:
            # плоская команда k=v: выводим её через тот же translator, чтобы
            # предпросмотр не врал «нет IR» там, где эффект явно назван
            from ct.core.state_ir import state_to_ir
            derived = state_to_ir(state, node.ecosystem.value)
            if derived is not None:
                self._desired_ir[device_id] = derived
        self.record_capture(device_id, caller)
        return result

    def desired_ir(self, device_id: str) -> Optional[EffectIR]:
        return self._desired_ir.get(device_id)

    async def command_group(self, group_id: str, state: dict, caller: Caller,
                            ir: Optional[EffectIR] = None) -> dict:
        g = self.groups[group_id]
        results = {}
        conflicts = []
        for dev_id in self.group_members(g):
            try:
                results[dev_id] = await self.command_device(dev_id, state, caller, ir)
            except ConflictError as e:
                conflicts.append(e.device_id)
        if conflicts:
            raise ConflictError(",".join(conflicts),
                                Capture(device_id="group", caller_id="other",
                                        role="admin", ts=time.time()))
        return {"group": group_id, "applied": results}

    def group_members(self, g: Group) -> list[str]:
        out = []
        for d in self.registry.all():
            if any(fnmatch.fnmatch(d.id, p) or d.labels.get("group") == p
                   for p in g.pattern):
                out.append(d.id)
        return sorted(out)

    # --- сцены / расписания ---------------------------------------------------
    async def run_scene(self, scene_id: str, caller: Caller) -> dict:
        scene = self.scenes[scene_id]
        applied = {}
        for step in scene.steps:
            ir_raw = step.get("ir")
            ir = EffectIR(**ir_raw) if isinstance(ir_raw, dict) else None
            if "group" in step:
                r = await self.command_group(step["group"], step.get("state", {}),
                                             caller, ir)
                applied.update(r.get("applied", {}))
            else:
                applied[step["device"]] = await self.command_device(
                    step["device"], step.get("state", {}), caller, ir)
        return {"scene": scene_id, "devices": list(applied)}

    async def _scheduler_loop(self) -> None:
        while True:
            now = time.localtime()
            for s in self.schedules.values():
                if not s.enabled:
                    continue
                if s.cronish.startswith("daily"):
                    hh, mm = map(int, s.cronish.split()[1].split(":"))
                    if (now.tm_hour, now.tm_min) == (hh, mm):
                        await self.run_scene(s.scene_id, Caller(id="scheduler", role="admin"))
                        await asyncio.sleep(60)  # не запускать дважды в минуту
            await asyncio.sleep(1)

    # --- реконсиляция desired vs reported --------------------------------------
    def report_state(self, device_id: str, state: dict) -> None:
        self._reported[device_id] = state

    def drift(self) -> dict[str, dict]:
        """Устройства, где reported != desired (плоское сравнение ключей)."""
        bad = {}
        for dev, want in self._desired.items():
            got = self._reported.get(dev, {})
            diff = {k: (want[k], got.get(k)) for k in want if got.get(k) != want[k]}
            if diff:
                bad[dev] = diff
        return bad

    async def reconcile(self) -> dict:
        """Дрейф правится повторной выдачей desired (подписка обновляет reported)."""
        fixed = {}
        for dev, diff in self.drift().items():
            desired = self._desired[dev]
            await self.apply(dev, desired, self._segment_for(dev), None)
            fixed[dev] = list(diff)
        return fixed

    # --- синхронизация кадра и часов (общий тик) --------------------------------
    async def start_tick(self, interval_s: float = 0.2) -> None:
        if self._tick_task and not self._tick_task.done():
            return
        self._tick_task = asyncio.create_task(self._tick_loop(interval_s))

    async def stop_tick(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
            self._tick_task = None

    async def _tick_loop(self, interval_s: float) -> None:
        synced_groups = [g for g in self.groups.values() if g.sync]
        while True:
            self._seq += 1
            payload = {"tick_seq": self._seq, "wall_ts": time.time(),
                       "period_ms": int(interval_s * 1000)}
            for g in synced_groups:
                for dev in self.group_members(g):
                    node = self.registry.get(dev)
                    if node and node.online.value == "online":
                        await self.apply(dev, {"sync": payload}, self._segment_for(dev), None)
            await asyncio.sleep(interval_s)

    def _segment_for(self, device_id: str) -> Optional[Segment]:
        for g in self.groups.values():
            if device_id in g.segments:
                return g.segments[device_id]
        return None
