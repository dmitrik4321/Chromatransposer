"""Инфраструктура: встроенный MQTT-брокер, HA-мост (без docker), discovery-экспорт."""
from __future__ import annotations

import pytest

from ct.core.ha_bridge import HaBridge
from ct.core.mqtt_broker import EmbeddedBroker


@pytest.mark.asyncio
async def test_embedded_broker_start_stop():
    # порт 18830 — чтобы не зависеть от занятого основного
    b = EmbeddedBroker({"enabled": True, "bind": "127.0.0.1:18830"})
    await b.start()
    assert b.running
    await b.stop()
    assert not b.running


@pytest.mark.asyncio
async def test_embedded_broker_disabled_is_noop():
    b = EmbeddedBroker({"enabled": False})
    await b.start()
    assert not b.running
    await b.stop()          # не должен упасть


def test_ha_off_mode():
    ha = HaBridge({"mode": "off"})
    st = ha.status()
    assert st["mode"] == "off" and st["ready"] is False


@pytest.mark.asyncio
async def test_ha_external_unreachable_sets_error():
    ha = HaBridge({"mode": "external", "host": "127.0.0.1", "port": 4}, )
    ok = await ha.ensure_running()
    assert ok is False
    assert "не отвечает" in ha.last_error


@pytest.mark.asyncio
async def test_server_start_with_embedded_broker(tmp_path):
    """Прослойка сама поднимает MQTT: сервер стартует без внешнего mosquitto."""
    import yaml
    from ct.core.server import Server
    cfg = {
        "mqtt": {"host": "127.0.0.1", "port": 18831,
                 "broker": {"enabled": True, "bind": "127.0.0.1:18831"}},
        "homeassistant": {"mode": "off"},
        "state_dir": str(tmp_path / "state"),
        "adapters": [], "groups": [], "scenes": [], "schedules": [],
    }
    p = tmp_path / "server.yaml"
    p.write_text(yaml.safe_dump(cfg))
    srv = Server(str(p))
    await srv.start()
    try:
        assert srv.broker.running
        st = srv.infra_status()
        assert st["mqtt_broker"]["embedded"] and st["mqtt_broker"]["running"]
        assert st["homeassistant"]["mode"] == "off"
    finally:
        await srv.stop()
    assert not srv.broker.running


@pytest.mark.asyncio
async def test_ha_discovery_publishes_light_config(tmp_path):
    """Discovery-экспорт: retained homeassistant/light/... конфиг в каноническом HA-формате."""
    import json as _json
    import yaml
    from ct.core.server import Server
    from ct.core.registry import DeviceNode, Ecosystem
    from ct.core.ir import Topology
    cfg = {
        "mqtt": {"host": "127.0.0.1", "port": 18832,
                 "broker": {"enabled": True, "bind": "127.0.0.1:18832"}},
        "homeassistant": {"mode": "off"},
        "state_dir": str(tmp_path / "state"),
        "adapters": [], "groups": [], "scenes": [], "schedules": [],
    }
    p = tmp_path / "server.yaml"
    p.write_text(yaml.safe_dump(cfg))
    srv = Server(str(p))
    await srv.start()
    try:
        node = DeviceNode(id="lamp1", ecosystem=Ecosystem.NATIVE_CT,
                          adapter="native_ct", name="Лампа",
                          topology=Topology(kind="strip", length=60))
        srv.registry.upsert(node)
        n = await srv.discovery.publish_all()
        assert n >= 1
        # подписываемся на discovered-топик и проверяем retain-конфиг
        got: dict[str, dict] = {}

        async def _collect(topic: str, payload: dict) -> None:
            got[topic] = payload

        srv.bridge.on("homeassistant/#", _collect)
        import asyncio
        await asyncio.sleep(0.3)
        key = "homeassistant/light/chromatransposer-lamp1/config"
        assert key in got, f"нет discovery-конфига; есть: {list(got)}"
        payload = got[key]
        assert payload["schema"] == "json"
        assert payload["availability_topic"].endswith("/available")
    finally:
        await srv.stop()
