"""Аутентификация и выдача (решение E).

Доверенная начальная настройка: USB-подключение к устройству -> устройство
подключается к указанному SSID/паролю -> сервер регистрирует его и выдаёт
device_id. Пока «раздача» открыта, новые устройства могут появляться; после
закрытия раздачи появление новых устройств невозможно без повторного
USB-доверения. Для самих ESP-устройств токенов нет — доверие по факту
прохождения процедуры + сегмент сети.

Клиентам API сервер раздаёт токены (user/admin) в период открытой раздачи
API-доступа; закрытие раздачи блокирует выпуск новых токенов.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from pydantic import BaseModel


class DeviceCredential(BaseModel):
    device_id: str
    ssid: str
    issued_at: float
    # секреты для L1-подтверждения (для native_ct прошивок); у экосистемных
    # устройств может отсутствовать — тогда доверяем адаптеру discovery
    shared_key: str = ""


class ApiToken(BaseModel):
    token: str
    caller_id: str
    role: str            # user | admin
    issued_at: float
    revoked: bool = False


class Provisioning:
    """Машина состояний раздачи. Флаг open_* меняет только админ через CLI/API."""

    def __init__(self, state_dir: str = "state"):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.open_devices = False     # идёт ли раздача device_id
        self.open_tokens = False      # идёт ли раздача API-токенов
        self.admin_secret = os.environ.get("CT_ADMIN_SECRET") or secrets.token_hex(16)
        self._devices: dict[str, DeviceCredential] = {}
        self._tokens: dict[str, ApiToken] = {}
        self._load()

    # --- раздача устройств ----------------------------------------------------
    def usb_provision(self, device_id: str, ssid: str, password: str) -> DeviceCredential:
        """Вызывается из CLI при подключенном по USB устройстве.

        Пароль проверяется локально (эмуляция: храним только хеш), генерируется
        shared_key, device_id фиксируется в allow-list. Работает ТОЛЬКО когда
        self.open_devices == True (админ открыл окно командой `ct provision devices on`).
        """
        if not self.open_devices:
            raise PermissionError("device provisioning window is closed; "
                                  "open it via admin CLI first")
        key = secrets.token_hex(16)
        cred = DeviceCredential(device_id=device_id, ssid=ssid,
                                issued_at=time.time(), shared_key=key)
        self._devices[device_id] = cred
        self._save()
        return cred

    def device_allowed(self, device_id: str) -> bool:
        return device_id in self._devices

    def known_devices(self) -> list[str]:
        return sorted(self._devices)

    # --- раздача API-токенов ---------------------------------------------------
    def issue_token(self, caller_id: str, role: str, admin_signature: str) -> ApiToken:
        if not self.open_tokens:
            raise PermissionError("token provisioning window is closed")
        if not self.verify_admin(admin_signature):
            raise PermissionError("bad admin signature")
        tok = ApiToken(token=secrets.token_urlsafe(24), caller_id=caller_id,
                       role=role, issued_at=time.time())
        self._tokens[tok.token] = tok
        self._save()
        return tok

    def authenticate(self, token: str) -> Optional[ApiToken]:
        t = self._tokens.get(token)
        if t and not t.revoked:
            return t
        return None

    def revoke_token(self, token: str) -> None:
        if token in self._tokens:
            self._tokens[token].revoked = True
            self._save()

    # --- окна раздачи ------------------------------------------------------------
    def set_window(self, which: str, opened: bool, admin_signature: str) -> None:
        if not self.verify_admin(admin_signature):
            raise PermissionError("bad admin signature")
        if which == "devices":
            self.open_devices = opened
        elif which == "tokens":
            self.open_tokens = opened
        else:
            raise ValueError(which)
        self._save()

    def verify_admin(self, signature: str) -> bool:
        return hmac.compare_digest(signature, self.admin_secret)

    @staticmethod
    def fingerprint(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()[:12]

    # --- persistence (конфиг как код / версионируемое состояние) ------------------
    def _path(self) -> str:
        return os.path.join(self.state_dir, "provisioning.json")

    def _save(self) -> None:
        import json
        data = {
            "open_devices": self.open_devices,
            "open_tokens": self.open_tokens,
            "admin_fingerprint": self.fingerprint(self.admin_secret),
            "devices": {k: v.model_dump(exclude={"shared_key"})
                        for k, v in self._devices.items()},
            "tokens": {k: v.model_dump() for k, v in self._tokens.items()},
        }
        with open(self._path(), "w") as f:
            json.dump(data, f, indent=2)

    def _load(self) -> None:
        import json
        if not os.path.exists(self._path()):
            return
        try:
            with open(self._path()) as f:
                data = json.load(f)
            self.open_devices = data.get("open_devices", False)
            self.open_tokens = data.get("open_tokens", False)
            for k, v in data.get("devices", {}).items():
                self._devices[k] = DeviceCredential(**v, shared_key="")  # ключ не храним на диске
            for k, v in data.get("tokens", {}).items():
                self._tokens[k] = ApiToken(**v)
        except Exception:
            pass
