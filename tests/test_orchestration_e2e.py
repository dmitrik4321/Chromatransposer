# ============================================================================
# E2E оркестрации через северный REST API (решения A/E + группы/сцены/расписания).
# Путь клиента: provisioning-окно -> токен -> REST -> оркестратор -> apply_fn,
# включая виртуальные пиксели (segment из команды доходит до «прошивки»),
# арбитраж admin-capture (409) и планировщик расписаний.
# Сервер поднимается с mode=off (без Docker/MQTT): проверяем именно HTTP-слой.
# Всё меняемое — в КОНФИГЕ ниже.
# ============================================================================
import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

# ---- КОНФИГ ----------------------------------------------------------------
SCHEDULE_EVERY = "every 1s"   # формат cronish планировщика
WAIT_S = 3.0                  # сколько ждать срабатывания every-расписания
API_PREFIX = "/api/v1"
HA_MODE = "off"               # без контейнера HA: тесты идут и на машине без Docker
BROKER_ENABLED = True         # брокер включён, но стартует на порту 0 (эфемерный) —
                              # параллельные прогоны/несколько инстансов не конфликтуют
ADMIN_SECRET = "test-admin-secret"
# -----------------------------------------------------------------------------


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """Реальный Server с отключённой инфраструктурой + шпионский apply_fn.

    apply перехватывается после start(): оркестратор вызывает self.apply,
    поэтому подмена атрибута работает без правки ядра. sent[] даёт факты
    «что реально ушло на юг» (state/segment), runs[] — число прогонов сцен.
    """
    from ct.core.server import Server
    monkeypatch.setenv("CT_ADMIN_SECRET", ADMIN_SECRET)
    server = Server(config_path="__missing__")          # конфига нет -> чистый SERVER_CONFIG
    server.config["homeassistant"] = {**server.config.get("homeassistant", {}),
                                      "mode": HA_MODE}
    server.config["mqtt"] = {**server.config.get("mqtt", {}),
                             "broker": {"enabled": BROKER_ENABLED, "bind": "127.0.0.1:0"}}
    server.broker.cfg["bind"] = "127.0.0.1:0"
    server.config["state_dir"] = str(tmp_path / "state")
    server.provisioning.state_dir = str(tmp_path / "state")

    real_apply = server._apply

    async def spy(device_id, state, segment, ir):
        r = await real_apply(device_id, state, segment, ir)
        server.sent.append({"dev": device_id, "state": dict(state),
                            "seg": segment.model_dump() if segment else None})
        return r

    server.sent: list[dict] = []
    server.runs: list[str] = []
    server._apply_spy = spy
    yield server


def _attach_spy(server):
    """Вешаем шпиона на оркестратор (он хранит apply_fn своим полем)."""
    server.orchestrator.apply = server._apply_spy


def _headers(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


@pytest.mark.asyncio
async def test_groups_scenes_schedules_via_rest(srv):
    """Полный CRUD групп/сцен/расписаний через HTTP + запуск сцены по расписанию."""
    import ct.north.rest_api as rest_api
    rest_api.server = srv
    try:
        await srv.start()
        _attach_spy(srv)
        # --- решение E: открыли окно раздачи токенов -> выдали -> закрыли
        srv.provisioning.open_tokens = True
        admin_tok = srv.provisioning.issue_token("boss", "admin", ADMIN_SECRET)
        user_tok = srv.provisioning.issue_token("guest", "user", ADMIN_SECRET)
        srv.provisioning.open_tokens = False
        with pytest.raises(PermissionError):
            srv.provisioning.issue_token("late", "user", ADMIN_SECRET)

        transport = ASGITransport(app=rest_api.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            ah = _headers(admin_tok.token)
            uh = _headers(user_tok.token)

            # регистрация двух native_ct-узлов напрямую в реестр (discovery не тестируем)
            from ct.core.ir import Topology
            from ct.core.registry import DeviceNode, Ecosystem
            for did in ("e2e-a", "e2e-b"):
                srv.registry.upsert(DeviceNode(
                    id=did, ecosystem=Ecosystem.NATIVE_CT, adapter="native_ct",
                    topology=Topology(kind="strip", length=64),
                    supports_ir=True, supports_segments=True))

            # 1) группа из двух узлов — топологию определяет только админ
            r = await client.post(f"{API_PREFIX}/groups", headers=uh, json={
                "id": "wall", "pattern": ["e2e-*"], "sync": True, "canvas_length": 128})
            assert r.status_code == 403, "топологию определяет только админ"
            r = await client.post(f"{API_PREFIX}/groups", headers=ah, json={
                "id": "wall", "pattern": ["e2e-*"], "sync": True, "canvas_length": 128})
            assert r.status_code == 200, r.text

            # 2) сцена: одна команда — множество узлов
            r = await client.put(f"{API_PREFIX}/scenes/e2e-on", headers=ah, json={
                "name": "on", "steps": [{"group": "wall", "state": {"on": True}}]})
            assert r.status_code == 200, r.text

            # 3) запуск сцены обычным пользователем: одна команда — оба узла
            r = await client.post(f"{API_PREFIX}/scenes/e2e-on/run", headers=uh)
            assert r.status_code == 200, r.text
            body = r.json()
            assert sorted(body["devices"]) == ["e2e-a", "e2e-b"], body
            scene_cmds = [s for s in srv.sent if s["state"] == {"on": True}]
            assert {s["dev"] for s in scene_cmds} == {"e2e-a", "e2e-b"}

            # 4) арбитраж (решение A): admin захватил e2e-a — user получает 409
            r = await client.post(f"{API_PREFIX}/devices/e2e-a/state", headers=ah,
                                  json={"state": {"bri": 200}})
            assert r.status_code == 200, r.text
            r = await client.post(f"{API_PREFIX}/devices/e2e-a/state", headers=uh,
                                  json={"state": {"bri": 10}})
            assert r.status_code == 409, r.text
            # release — и user снова может писать
            r = await client.post(f"{API_PREFIX}/devices/e2e-a/release", headers=ah)
            assert r.status_code == 200
            r = await client.post(f"{API_PREFIX}/devices/e2e-a/state", headers=uh,
                                  json={"state": {"bri": 10}})
            assert r.status_code == 200, r.text

            # 5) виртуальные пиксели: segment из HTTP-команды доезжает до юга
            srv.sent.clear()
            r = await client.post(f"{API_PREFIX}/devices/e2e-b/state", headers=uh,
                                  json={"state": {"on": True},
                                        "segment": {"device_id": "e2e-b",
                                                    "start": 64, "length": 64,
                                                    "canvas_offset": 64}})
            assert r.status_code == 200, r.text
            seg = srv.sent[-1]["seg"]
            assert seg is not None and seg["start"] == 64 and seg["canvas_offset"] == 64

            # 6) расписание every Ns: создали — цикл сам запускает сцену
            runs_before = len(srv.sent)
            r = await client.put(f"{API_PREFIX}/schedules/every-test", headers=ah,
                                 json={"scene_id": "e2e-on",
                                       "cronish": SCHEDULE_EVERY})
            assert r.status_code == 200, r.text
            await asyncio.sleep(WAIT_S)
            sched_cmds = [s for s in srv.sent[runs_before:] if s["state"] == {"on": True}]
            assert {s["dev"] for s in sched_cmds} == {"e2e-a", "e2e-b"}, \
                "планировщик не запустил сцену по расписанию"

            # 7) list/delete — обратная связь консоли
            r = await client.get(f"{API_PREFIX}/groups", headers=uh)
            assert "wall" in r.json()
            r = await client.delete(f"{API_PREFIX}/schedules/every-test", headers=ah)
            assert r.status_code == 200
            r = await client.get(f"{API_PREFIX}/schedules", headers=uh)
            assert "every-test" not in r.json()
    finally:
        await srv.stop()
        rest_api.server = None
