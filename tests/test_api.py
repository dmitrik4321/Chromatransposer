"""E2E-тест северного API на fake-сервере (без MQTT/устройств).

Проверяет: auth-токены, 401/403, выдачу устройств через окно раздачи,
команды с сегментами, 409 при админ-захвате, port-эндпоинт с потерями.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ct.core.ir import EffectIR, Segment, Topology
from ct.core.registry import DeviceNode, Ecosystem, Registry
from ct.north import rest_api
from ct.south_cli.cli import _kv


class FakeServer:
    """Минимальная замена Server: registry + orchestrator + provisioning."""

    def __init__(self):
        from ct.core.auth import Provisioning
        from ct.core.orchestrator import Orchestrator
        self.sent = []

        async def apply(dev, state, seg, ir):
            self.sent.append({"dev": dev, "state": state,
                              "seg": seg.model_dump() if seg else None})
            return {"ok": dev}

        self.registry = Registry()
        self.provisioning = Provisioning(state_dir="/tmp/ct-test-state")
        self.orchestrator = Orchestrator(self.registry, apply)
        self.telemetry_latest = {}
        node = DeviceNode(id="strip-ct", ecosystem=Ecosystem.NATIVE_CT,
                          adapter="native_ct",
                          topology=Topology(kind="strip", length=120),
                          supports_ir=True, supports_segments=True)
        self.registry.upsert(node)

    def _adapter_for(self, node):
        raise NotImplementedError

    def port_native(self, ref, target_id, args=None):
        from ct.core.translator import native_to_ir, ir_to_native
        return ir_to_native(native_to_ir(ref, args), self.registry.get(target_id))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    srv = FakeServer()
    # окна раздачи открыты, чтобы выдать токены
    srv.provisioning.state_dir = str(tmp_path)
    srv.provisioning.open_tokens = True
    srv.provisioning.open_devices = True
    tok = srv.provisioning.issue_token("tester", "user",
                                       srv.provisioning.admin_secret)
    atok = srv.provisioning.issue_token("boss", "admin",
                                        srv.provisioning.admin_secret)
    app = FastAPI()
    app.router.routes.extend(rest_api.app.routes)
    rest_api.server = srv
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {tok.token}"})
    c_admin = TestClient(app)
    c_admin.headers.update({"Authorization": f"Bearer {atok.token}"})
    yield c, c_admin, srv
    rest_api.server = None


def test_requires_token(client):
    c, _, _ = client
    c.headers.pop("Authorization")
    assert c.get("/api/v1/devices").status_code == 401


def test_unknown_token_rejected(client):
    c, _, _ = client
    c.headers["Authorization"] = "Bearer nope"
    assert c.get("/api/v1/devices").status_code == 403


def test_list_and_command_with_segment(client):
    c, _, srv = client
    devs = c.get("/api/v1/devices").json()
    assert devs[0]["id"] == "strip-ct"
    r = c.post("/api/v1/devices/strip-ct/state", json={
        "state": {"on": True},
        "segment": {"device_id": "strip-ct", "start": 40, "length": 30}})
    assert r.status_code == 200
    assert srv.sent[-1]["seg"]["start"] == 40


def test_apply_ir_direct(client):
    c, _, srv = client
    ir = {"class": "perlin", "params": {"speed": 0.3}}
    r = c.post("/api/v1/devices/strip-ct/state", json={"ir": ir})
    assert r.status_code == 200


def test_admin_capture_conflict(client):
    c, c_admin, _ = client
    assert c_admin.post("/api/v1/devices/strip-ct/state",
                        json={"state": {"on": True}}).status_code == 200
    blocked = c.post("/api/v1/devices/strip-ct/state", json={"state": {"on": False}})
    assert blocked.status_code == 409
    # админ снимает захват — user снова может
    c_admin.post("/api/v1/devices/strip-ct/release")
    assert c.post("/api/v1/devices/strip-ct/state",
                  json={"state": {"on": False}}).status_code == 200


def test_port_endpoint_declares_losses(client):
    c, _, _ = client
    body = {"source": {"ecosystem": "wled", "effect": "Fire2012",
                       "args": {"sx": 200}},
            "target_device": "strip-ct"}
    pr = c.post("/api/v1/port", json=body).json()
    assert pr["ir"]["class"] == "fire"
    assert pr["target_native"]["effect"] == "fire"   # native_ct ест IR прямо


def test_provision_usb_window(client):
    c, _, srv = client
    srv.provisioning.open_devices = True
    r = c.post("/api/v1/provision/device",
               json={"device_id": "esp-new", "ssid": "home24", "wifi_password": "pw"})
    assert r.status_code == 200
    assert "shared_key" in r.json()
    srv.provisioning.open_devices = False
    r2 = c.post("/api/v1/provision/device",
                json={"device_id": "esp-sneaky", "ssid": "home24", "wifi_password": "pw"})
    assert r2.status_code == 403   # закрытая раздача -> новых устройств нет


def test_cli_kv_parsing():
    assert _kv(["on=true", "bri=128", "name=кухня"]) == {
        "on": True, "bri": 128, "name": "кухня"}
