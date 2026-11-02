"""Home Assistant: прослойка сама поднимает HA-сервер (требование к MVP).

Стратегия (без изменения исходников HA):
  * mode="embedded"  — контейнер homeassistant/home-assistant через docker;
    если docker недоступен, пробуем podman; если и он нет — понятная ошибка
    с инструкцией. Это НЕ критично для ядра: сервер стартует и работает,
    HA поднимается фоном и retry-ется.
  * mode="external"  — HA уже запущен пользователем; мы только проверяем
    доступность http://host:port.
  * mode="off"       — ничего не трогаем.

Интеграция Chromatransposer <-> HA — discovery-экспорт в MQTT-канон
homeassistant/# (см. ct/core/ha_discovery.py), который HA съедает родным
mqtt-интегратором. Никаких форков и плагинов в сам HA.

Всё меняемое — в HA_CONFIG ниже; YAML-секция homeassistant поверх.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from typing import Optional

# ------------------------------ КОНФИГ HA -------------------------------------
HA_CONFIG = {
    "mode": "embedded",                 # embedded | external | off
    "container": "chromatransposer-ha", # имя контейнера (легко менять/удалять)
    "image": "homeassistant/home-assistant:latest",
    "host": "127.0.0.1",                # для external-проверки и REST-вызовов
    "port": 8123,
    "config_dir": "state/hass_config",  # persist конфигов HA рядом с состоянием
    "token_file": "state/ha_token",     # long-lived токен (создаётся вручную/CLI)
    "startup_timeout_s": 90,            # сколько ждать подъёма веб-интерфейса
    "restart_policy": "unless-stopped",
}
# -----------------------------------------------------------------------------


class HaBridge:
    """Управление локальным HA: старт контейнера, проверка готовности, статус."""

    def __init__(self, cfg: Optional[dict] = None, mqtt_bind: str = ""):
        self.cfg = {**HA_CONFIG, **(cfg or {})}
        self.mqtt_bind = mqtt_bind      # "127.0.0.1:1883" — куда HA смотреть брокер
        self.last_error: str = ""

    # --- выбор рантайма контейнеров -------------------------------------------
    @staticmethod
    def _runtime() -> Optional[str]:
        for rt in ("docker", "podman"):
            if shutil.which(rt):
                return rt
        return None

    def status(self) -> dict:
        mode = self.cfg["mode"]
        ready = self._probe_sync()
        return {"mode": mode, "ready": ready,
                "url": f"http://{self.cfg['host']}:{self.cfg['port']}",
                "runtime": self._runtime(), "error": self.last_error,
                "token_present": os.path.exists(self.cfg["token_file"])}

    # --- синхронная проверка доступности (для CLI/status) ----------------------
    def _probe_sync(self) -> bool:
        if self.cfg["mode"] == "off":
            return False
        try:
            import urllib.request
            with urllib.request.urlopen(
                    f"http://{self.cfg['host']}:{self.cfg['port']}/", timeout=2):
                return True
        except Exception:
            return False

    # --- запуск ----------------------------------------------------------------
    async def ensure_running(self) -> bool:
        """Не блокирует старт сервера: ошибки пишутся в last_error."""
        mode = self.cfg["mode"]
        if mode == "off":
            return False
        if await asyncio.get_running_loop().run_in_executor(None, self._probe_sync):
            return True                       # уже живёт (или external)
        if mode == "external":
            self.last_error = ("HA (external) не отвечает на "
                               f"{self.cfg['host']}:{self.cfg['port']}")
            return False
        # embedded
        rt = self._runtime()
        if rt is None:
            self.last_error = ("не найден docker/podman: поставь Docker или "
                               "поставь homeassistant.mode: external/off")
            return False
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._start_container(rt))

    def _start_container(self, rt: str) -> bool:
        name, image = self.cfg["container"], self.cfg["image"]
        cdir = os.path.abspath(self.cfg["config_dir"])
        os.makedirs(cdir, exist_ok=True)
        # если контейнер уже существует — просто стартуем его повторно
        exists = subprocess.run([rt, "container", "inspect", name],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL).returncode == 0
        if exists:
            rc = subprocess.run([rt, "container", "start", name],
                                capture_output=True, text=True)
            if rc.returncode != 0:
                self.last_error = rc.stderr.strip()
                return False
            return True
        args = [rt, "run", "-d", "--name", name,
                "--restart", self.cfg["restart_policy"],
                "-v", f"{cdir}:/config",
                "-e", "PUID=0", "-e", "PGID=0",
                # host-сеть: HA видит mDNS-устройства и встроенный брокер
                "--net", "host",
                image]
        try:
            rc = subprocess.run(args, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            self.last_error = "docker run превысил 120 c (тяжелый образ?)"
            return False
        if rc.returncode != 0:
            self.last_error = (rc.stderr or rc.stdout).strip()[:400]
            return False
        return True

    async def wait_ready(self) -> bool:
        deadline = self.cfg["startup_timeout_s"]
        waited = 0.0
        while waited < deadline:
            if await asyncio.get_running_loop().run_in_executor(
                    None, self._probe_sync):
                return True
            await asyncio.sleep(2)
            waited += 2
        self.last_error = f"HA не поднялся за {deadline}s"
        return False

    # --- токен для REST (discovery-экспорт опционально использует) -------------
    def read_token(self) -> str:
        try:
            with open(self.cfg["token_file"]) as f:
                return f.read().strip()
        except OSError:
            return ""

    def write_token(self, token: str) -> None:
        os.makedirs(os.path.dirname(self.cfg["token_file"]) or ".", exist_ok=True)
        with open(self.cfg["token_file"], "w") as f:
            f.write(token)
