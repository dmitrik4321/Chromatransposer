# ============================================================================
# E2E оркестрации через северный REST API (решения A/B + группы/сцены/расписания).
# Проверяем то, что раньше тестировалось только на уровне ядра: путь
# CLI/клиент -> REST -> оркестратор -> адаптер, включая виртуальные пиксели
# (segment-маппинг из группы доходит до прошивки) и sync-tick.
# Всё меняемое — в КОНФИГЕ ниже.
# ============================================================================
import asyncio

import httpx
import pytest

# ---- КОНФИГ ----------------------------------------------------------------
TICK_HZ = 5           # частота общего тика (решение B)
TICK_WAIT_S = 1.0     # сколько ждать рассылки тиков
API_PREFIX = "/api/v1"
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_groups_scenes_schedules_via_rest(local_server_app):
    """Полный CRUD групп/сцен/расписаний через HTTP + запуск сцены по расписанию."""
    server, app = local_server_app
    async with app.test() as client:  # httpx ASGI client
        h = {"Authorization": f"Bearer {server.api_token}"}

        # 1) группа из двух native_ct-узлов с сегментами (виртуальные пиксели)
        r = await client.post(f"{API_PREFIX}/groups", headers=h, json={
            "id": "wall", "pattern": ["e2e-*"], "sync": True,
            "canvas_length": 128,
            "segments": {"e2e-a": {"device_id": "e2e-a", "start": 0, "length": 64, "canvas_offset": 0},
                         "e2e-b": {"device_id": "e2e-b", "start": 64, "length": 64, "canvas_offset": 64}},
        })
        assert r.status_code == 200, r.text

        # 2) сцена: включить группу целиком одной командой
        r = await client.put(f"{API_PREFIX}/scenes/e2e-on", headers=h, json={
            "name": "on", "steps": [{"group": "wall", "state": {"on": True}}]})
        assert r.status_code == 200, r.text

        # 3) запуск сцены через REST
        r = await client.post(f"{API_PREFIX}/scenes/e2e-on/run", headers=h)
        assert r.status_code == 200, r.text
        results = r.json()["results"]
        assert len(results) == 2, results
        # сегменты из группы доехали до «прошивки» (адаптер пишет seg в state)
        by_dev = {res["device"]: res for res in results}
        assert by_dev["e2e-a"]["seg_start"] == 0 and by_dev["e2e-b"]["seg_start"] == 64

        # 4) арбитраж (решение A): админ захватил e2e-a — user получает 409
        admin_h = {"Authorization": f"Bearer {server.api_token}"}
        r = await client.post(f"{API_PREFIX}/devices/e2e-a/state?role=admin",
                              headers=admin_h, json={"state": {"bri": 200}})
        assert r.status_code == 200, r.text
        r = await client.post(f"{API_PREFIX}/devices/e2e-a/state?role=user",
                              headers=admin_h, json={"state": {"bri": 10}})
        assert r.status_code == 409, r.text
        # release — и user снова может писать
        r = await client.post(f"{API_PREFIX}/devices/e2e-a/release?role=admin", headers=admin_h)
        assert r.status_code == 200
        r = await client.post(f"{API_PREFIX}/devices/e2e-a/state?role=user",
                              headers=admin_h, json={"state": {"bri": 10}})
        assert r.status_code == 200, r.text

        # 5) расписание every Ns: создали, включили тик — сцена сработала сама
        r = await client.post(f"{API_PREFIX}/schedules", headers=h, json={
            "id": "every-test", "scene_id": "e2e-on", "cronish": "every 1s"})
        assert r.status_code == 200, r.text
        before = server.orchestrator._scene_runs.get("e2e-on", 0)
        await server.orchestrator.start_tick(1.0 / TICK_HZ)
        try:
            await asyncio.sleep(TICK_WAIT_S * 2)
        finally:
            await server.orchestrator.stop_tick()
        after = server.orchestrator._scene_runs.get("e2e-on", 0)
        assert after > before, "планировщик не запустил сцену по расписанию"

        # 6) list/delete — обратная связь консоли
        r = await client.get(f"{API_PREFIX}/groups", headers=h)
        assert "wall" in r.json()
        r = await client.delete(f"{API_PREFIX}/schedules/every-test", headers=h)
        assert r.status_code == 200
        r = await client.get(f"{API_PREFIX}/schedules", headers=h)
        assert "every-test" not in r.json()
