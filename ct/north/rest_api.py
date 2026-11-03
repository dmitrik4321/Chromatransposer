"""Северный API: REST (FastAPI) + WebSocket. Единая поверхность для клиентов;
гетерогенность скрыта адаптерами и IR. Аутентификация — Bearer-токены,
выданные через Provisioning (решение E). Роли user/admin (решение A).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ct.adapters.base import UnsupportedError
from ct.core.ir import EffectIR, NativeRef, Segment
from ct.core.orchestrator import Caller, ConflictError, Group, Scene, Schedule
from ct.core.server import Server

app = FastAPI(title="Chromatransposer", version="0.1.0")
server: Optional[Server] = None  # инжектится в main.py


def get_caller(authorization: str = Header(default="")) -> Caller:
    tok = authorization.removeprefix("Bearer ").strip()
    if not tok:
        raise HTTPException(401, "missing bearer token")
    api_tok = server.provisioning.authenticate(tok)
    if api_tok is None:
        raise HTTPException(403, "unknown/revoked token")
    return Caller(id=api_tok.caller_id, role=api_tok.role)


class StateBody(BaseModel):
    state: dict = {}
    ir: Optional[dict] = None
    segment: Optional[dict] = None


class PortBody(BaseModel):
    source: dict          # {ecosystem, effect, args}
    target_device: str


class TokenIssue(BaseModel):
    caller_id: str
    role: str = "user"
    admin_signature: str


class WindowBody(BaseModel):
    which: str            # devices | tokens
    opened: bool
    admin_signature: str


class UsbProvisionBody(BaseModel):
    device_id: str
    ssid: str
    wifi_password: str


@app.get("/api/v1/devices")
def devices(caller: Caller = Depends(get_caller)):
    return [d.model_dump() for d in server.registry.all()]


@app.get("/api/v1/devices/{device_id}")
def device(device_id: str, caller: Caller = Depends(get_caller)):
    node = server.registry.get(device_id)
    if node is None:
        raise HTTPException(404, "no such device")
    return node.model_dump()


@app.get("/api/v1/devices/{device_id}/state")
async def device_state(device_id: str, caller: Caller = Depends(get_caller)):
    node = server.registry.get(device_id)
    if node is None:
        raise HTTPException(404)
    adapter = server._adapter_for(node)
    return await adapter.get_state(device_id)


@app.post("/api/v1/devices/{device_id}/state")
async def set_device(device_id: str, body: StateBody,
                     caller: Caller = Depends(get_caller)):
    ir = EffectIR(**body.ir) if body.ir else None
    seg = Segment(**body.segment) if body.segment else None
    try:
        return await server.orchestrator.command_device(device_id, body.state, caller,
                                                         ir, segment=seg)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ConflictError as e:
        raise HTTPException(409, str(e))
    except UnsupportedError as e:
        raise HTTPException(422, str(e))


@app.post("/api/v1/devices/{device_id}/release")
def release(device_id: str, caller: Caller = Depends(get_caller)):
    server.orchestrator.release_capture(device_id, caller)
    return {"released": device_id}


@app.get("/api/v1/capabilities")
def capabilities(caller: Caller = Depends(get_caller)):
    """Что умеет парк в IR-терминах: классы эффектов по устройствам."""
    out = {}
    for d in server.registry.all():
        out[d.id] = {"ecosystem": d.ecosystem.value, "topology": d.topology.model_dump(),
                     "supports_ir": d.supports_ir, "supports_segments": d.supports_segments,
                     "effects": [e.effect for e in d.native_effects],
                     "telemetry": d.telemetry_kinds}
    return out


@app.get("/api/v1/effect_catalog")
def effect_catalog(caller: Caller = Depends(get_caller)):
    from ct.core.ir import EffectClass
    from ct.core.translator import ECOSYSTEM_MAPS
    return {"classes": [c.value for c in EffectClass],
            "by_ecosystem": {eco: sorted(m.keys()) for eco, m in ECOSYSTEM_MAPS.items()}}


@app.post("/api/v1/port")
async def port_effect(body: PortBody, caller: Caller = Depends(get_caller)):
    """Портирование native(src) -> IR -> native(dst) с декларацией потерь."""
    ref = NativeRef(**body.source)
    pr = server.port_native(ref, body.target_device)
    # сериализация по алиасам: EffectIR.class выходит как "class"
    return json.loads(pr.model_dump_json(by_alias=True))


# --- groups / scenes / schedules ---------------------------------------------
class SceneBody(BaseModel):
    name: str = ""
    steps: list[dict] = []
    fade_ms: int = 0


class ScheduleBody(BaseModel):
    scene_id: str
    cronish: str            # "daily HH:MM" | "every Ns" | "at ISO"
    enabled: bool = True


@app.get("/api/v1/groups")
def groups(caller: Caller = Depends(get_caller)):
    return {gid: g.model_dump() for gid, g in server.orchestrator.groups.items()}


@app.post("/api/v1/groups")
def put_group(g: Group, caller: Caller = Depends(get_caller)):
    if not caller.is_admin:
        raise HTTPException(403, "admin required to define topology")
    server.orchestrator.groups[g.id] = g
    return {"ok": g.id}


@app.delete("/api/v1/groups/{group_id}")
def del_group(group_id: str, caller: Caller = Depends(get_caller)):
    if not caller.is_admin:
        raise HTTPException(403, "admin required")
    server.orchestrator.groups.pop(group_id, None)
    return {"removed": group_id}


@app.get("/api/v1/scenes")
def scenes(caller: Caller = Depends(get_caller)):
    return {sid: s.model_dump() for sid, s in server.orchestrator.scenes.items()}


@app.put("/api/v1/scenes/{scene_id}")
def put_scene(scene_id: str, body: SceneBody, caller: Caller = Depends(get_caller)):
    if not caller.is_admin:
        raise HTTPException(403, "admin required to define scenes")
    server.orchestrator.scenes[scene_id] = Scene(id=scene_id, **body.model_dump())
    return {"ok": scene_id}


@app.delete("/api/v1/scenes/{scene_id}")
def del_scene(scene_id: str, caller: Caller = Depends(get_caller)):
    if not caller.is_admin:
        raise HTTPException(403, "admin required")
    server.orchestrator.scenes.pop(scene_id, None)
    return {"removed": scene_id}


@app.get("/api/v1/schedules")
def schedules(caller: Caller = Depends(get_caller)):
    return {scid: s.model_dump() for scid, s in server.orchestrator.schedules.items()}


@app.put("/api/v1/schedules/{schedule_id}")
def put_schedule(schedule_id: str, body: ScheduleBody,
                 caller: Caller = Depends(get_caller)):
    if not caller.is_admin:
        raise HTTPException(403, "admin required")
    if body.scene_id not in server.orchestrator.scenes:
        raise HTTPException(404, "unknown scene")
    server.orchestrator.schedules[schedule_id] = Schedule(id=schedule_id, **body.model_dump())
    return {"ok": schedule_id}


@app.delete("/api/v1/schedules/{schedule_id}")
def del_schedule(schedule_id: str, caller: Caller = Depends(get_caller)):
    if not caller.is_admin:
        raise HTTPException(403, "admin required")
    server.orchestrator.schedules.pop(schedule_id, None)
    return {"removed": schedule_id}


@app.post("/api/v1/groups/{group_id}/state")
async def group_state(group_id: str, body: StateBody,
                      caller: Caller = Depends(get_caller)):
    ir = EffectIR(**body.ir) if body.ir else None
    try:
        return await server.orchestrator.command_group(group_id, body.state, caller, ir)
    except KeyError:
        raise HTTPException(404, "no such group")
    except ConflictError as e:
        raise HTTPException(409, str(e))


@app.post("/api/v1/scenes/{scene_id}/run")
async def run_scene(scene_id: str, caller: Caller = Depends(get_caller)):
    try:
        return await server.orchestrator.run_scene(scene_id, caller)
    except KeyError:
        raise HTTPException(404, "no such scene")
    except ConflictError as e:
        raise HTTPException(409, str(e))


@app.post("/api/v1/reconcile")
async def reconcile(caller: Caller = Depends(get_caller)):
    return await server.orchestrator.reconcile()


@app.get("/api/v1/telemetry")
def telemetry(caller: Caller = Depends(get_caller)):
    return server.telemetry_latest


@app.get("/api/v1/render/snapshot")
def render_snapshot(caller: Caller = Depends(get_caller)):
    """Кадры ASCII-предпросмотра для удалённого CLI.

    Честно: это ПРЕДПОЛОЖЕНИЕ сервера по desired-IR (control plane), а не
    снимок реальной ленты; точность зависит от адаптеров.
    """
    from ct.core.render import classify_device, render_device_frame
    t = time.time()
    out = []
    for node in server.registry.all():
        ir = server.orchestrator.desired_ir(node.id)
        segs = {}
        for g in server.orchestrator.groups.values():
            segs.update(g.segments)
        lines = render_device_frame(node, ir, t, seg_map=segs)
        out.append({"device": node.id, "type": classify_device(node),
                    "lines": lines})
    return {"t": t, "devices": out}


@app.get("/api/v1/status")
def status(caller: Caller = Depends(get_caller)):
    """Состояние инфраструктуры: встроенный MQTT-брокер и HA (требование:
    прослойка сама поднимает оба)."""
    return {"ok": True, **server.infra_status()}


# --- provisioning ------------------------------------------------------------
@app.post("/api/v1/provision/windows")
def provision_window(body: WindowBody):
    try:
        server.provisioning.set_window(body.which, body.opened, body.admin_signature)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return {"open_devices": server.provisioning.open_devices,
            "open_tokens": server.provisioning.open_tokens}


@app.post("/api/v1/provision/device")
def provision_usb(body: UsbProvisionBody):
    """Вызывается локальной CLI-программой при USB-подключении устройства."""
    try:
        cred = server.provisioning.usb_provision(
            body.device_id, body.ssid, body.wifi_password)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return {"device_id": cred.device_id, "shared_key": cred.shared_key}


@app.post("/api/v1/provision/token")
def provision_token(body: TokenIssue):
    try:
        tok = server.provisioning.issue_token(body.caller_id, body.role,
                                              body.admin_signature)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return tok.model_dump()


# --- WebSocket: события реестра/состояний --------------------------------------
@app.websocket("/api/v1/ws")
async def ws_endpoint(ws: WebSocket):
    tok = ws.query_params.get("token", "")
    api_tok = server.provisioning.authenticate(tok) if server else None
    if api_tok is None:
        await ws.close(code=4403)
        return
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue()

    def on_change(device_id: str):
        node = server.registry.get(device_id)
        if node:
            queue.put_nowait({"event": "device", "device": node.model_dump()})

    server.registry.subscribe(on_change)
    try:
        while True:
            msg = await queue.get()
            await ws.send_json(msg)
    except WebSocketDisconnect:
        pass
