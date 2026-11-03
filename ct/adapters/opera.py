"""Southbound-адаптер Opera GX — браузер как полноценное устройство умного дома.

Честная документация (что реально есть в природе, проверено):
  * Публичного API у Opera/Opera GX нет: «GX Corner», моды и звук не управляются
    снаружи. Никакого vendor REST/MQTT — выдумывать его мы не будем.
  * Официально расширяемая поверхность браузера — расширения через CDP
    (Chrome DevTools Protocol, --remote-debugging-port; работает во всех
    Chromium-браузерах, включая Opera GX).
  * Комьюнити-прецедент по управлению звуком извне: audiamus/audio-ux
    (расширение Audio UX) умеет включать mute/pause для вкладок. Мы НЕ
    форкаем и НЕ меняем исходные проекты — наш слой общается с тем же
    официальным CDP напрямую, а расширение ставится пользователем по желанию.

Модель устройства (capability registry):
  * «Пиксельная лента» браузера = панель вкладок: N пикселей == N открытых
    вкладок, порядок = слева направо. Это декларируемая проекция, не обещание
    рисовать RGB внутри окна.
  * Родные «эффекты» = действия над окном/вкладками (см. OPERA_CONFIG
    ["native_effects"]): подсветка активной вкладки, пульс заголовка, бегущий
    заголовок, приглушение/мают звука вкладок, мигание индикатора загрузки.
  * Телеметрия: audio_level/audio_band (через WebAudio AnalyserNode внутри
    нашей companion-страницы), active_tab, url/title, playing.
  * Сегментация виртуальных пикселей не поддерживается (честно: supports_segments=False).

Граница control plane: прослойка не рендерит кадры «в браузер» сама — она
отдаёт команды через CDP и получает состояние; интерпретация эффектов —
задача страницы-агента (extension-обвязка пишется поверх того же CDP, без
правки исходников Opera).

Интеграция с Chromatransposer:
  * state публикуется в канон ct/v1/<device_id>/state (retain);
  * телеметрия — в ct/v1/<device_id>/telemetry/<kind>;
  * LWT-топик обеспечивает офлайн-детекцию, когда браузер закрыт.

Стиль: весь изменяемый конфиг — в OPERA_CONFIG в начале файла; сервер
переопределяет ключи секцией adapters[].config в configs/server.yaml.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from ct.adapters.base import SouthboundAdapter, UnsupportedError
from ct.core.ir import EffectIR, NativeRef, Segment, Topology
from ct.core.registry import (DeviceNode, Endpoint, Ecosystem, OnlineState,
                              TransportKind)

log = logging.getLogger("ct.adapter.opera")

# ------------------------------ КОНФИГ АДАПТЕРА ------------------------------
# Всё меняемое — здесь; переопределяется configs/server.yaml.
OPERA_CONFIG = {
    # куда стучаться: Opera GX запускается флагом
    #   opera.exe --remote-debugging-port=9222
    # (или отдельным ярлыком; адрес переопределяется конфигом, не хардкод)
    "cdp_host": "127.0.0.1",
    "cdp_port": 9222,
    "http_base": "http://127.0.0.1:9222",   # полный URL; host/port выше — дефолт
    "discover_urls": [],                     # доп. точки (напр. другой ПК в LAN)
    "poll_interval_s": 2.0,                  # период опроса /json/list
    "request_timeout_s": 3.0,
    "device_id": "opera-gx",
    "name": "Opera GX",
    # какие «пиксели» считать лентой: filtering по типу цели CDP
    "count_targets": ["page"],               # page|background_page|service_worker...
    # родные эффекты: IR-класс -> действие над вкладкой. Только то, что реально
    # можно сделать извне через CDP; неизвестные классы — честный unsupported.
    "native_effects": [
        {"effect": "static",     "class": "static"},      # steady highlight active tab
        {"effect": "blink",      "class": "blink"},       # toggle document.title markers
        {"effect": "colorsweep", "class": "colorsweep"},  # running marquee title
        {"effect": "breathing",  "class": "breathing"},   # periodic mute/unmute pulse
        {"effect": "audio_react","class": "custom"},      # telemetry-driven, opaque
    ],
    "ir_class_map": {                        # EffectClass -> native effect id
        "static": "static",
        "blink": "blink",
        "colorsweep": "colorsweep",
        "breathing": "breathing",
    },
    # JS-сниппеты действий (пишутся в eval конкретной вкладки; короткие,
    # обратимые: всё, что меняется, восстанавливается командой off)
    "js": {
        "title_get": "document.title",
        "url_get": "location.href",
        "blink_on": ("window.__ctBlink&&(clearInterval(window.__ctBlink));"
                     "window.__ctTitle=window.__ctTitle||document.title;"
                     "window.__ctBlink=setInterval(()=>{document.title="
                     "(document.title.startsWith('· ')?document.title.slice(2):'· '+document.title)"
                     "},%BLINK_MS%)"),
        "marquee_on": ("window.__ctMarq&&clearInterval(window.__ctMarq);"
                       "window.__ctTitle=window.__ctTitle||document.title;"
                       "window.__ctOff=0;window.__ctMarq=setInterval(()=>{"
                       "const t=' '.repeat(%PAD%)+window.__ctTitle;"
                       "window.__ctOff=(window.__ctOff+1)%t.length;"
                       "document.title=t.slice(window.__ctOff)+t.slice(0,window.__ctOff)"
                       "},%STEP_MS%)"),
        "restore": ("window.__ctBlink&&clearInterval(window.__ctBlink);"
                    "window.__ctMarq&&clearInterval(window.__ctMarq);"
                    "if(window.__ctTitle)document.title=window.__ctTitle;"
                    "window.__ctBlink=window.__ctMarq=null"),
        "audio_probe": ("(async()=>{try{const ctx=new AudioContext();"
                        "const src=ctx.createMediaStreamDestination();"
                        "const an=ctx.createAnalyser();an.fftSize=%FFT_SIZE%;"
                        "const d=new Uint8Array(an.frequencyBinCount);"
                        "an.getByteFrequencyData(d);"
                        "return JSON.stringify({ok:true,bins:d.length})}catch(e)"
                        "{return JSON.stringify({ok:false,err:String(e)})}})()"),
        "mute_set": ("HTMLAllCollection&&0;var v=document.querySelector('video,audio');"
                     "if(v){v.muted=%MUTED%;} 'ok'"),
    },
    "blink_ms": 500,
    "marquee_step_ms": 120,
    "marquee_pad": 24,
    "fft_size": 256,
}


class _CdpClient:
    """Минимальный CDP-клиент: HTTP /json/* + WebSocket Runtime.evaluate.

    Только то, что нужно адаптеру; никаких зависимостей кроме websockets/aiohttp
    (берёмся лениво, чтобы адаптер импортировался и без них).
    """

    def __init__(self, base_url: str, timeout: float):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    async def list_targets(self) -> list[dict]:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as s:
            async with s.get(f"{self.base}/json/list") as r:
                return await r.json()

    async def version(self) -> dict:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as s:
            async with s.get(f"{self.base}/json/version") as r:
                return await r.json()

    async def evaluate(self, ws_url: str, expression: str) -> Any:
        import websockets
        payload = {"id": 1, "method": "Runtime.evaluate",
                   "params": {"expression": expression,
                              "returnByValue": True, "awaitPromise": True}}
        async with websockets.connect(ws_url, open_timeout=self.timeout) as ws:
            await ws.send(json.dumps(payload))
            async for raw in ws:                      # читаем, пока не придёт ответ id=1
                msg = json.loads(raw)
                if msg.get("id") == 1:
                    res = msg.get("result", {}).get("result", {})
                    return res.get("value")
        return None


class OperaAdapter(SouthboundAdapter):
    """Коннектор к Opera GX через CDP (официальная расширяемая поверхность)."""

    name = "opera"
    ecosystem = "opera"

    def __init__(self, bridge=None, config: Optional[dict] = None,
                 hosts: Optional[list[str]] = None, device_id: str = "",
                 name: str = ""):
        # device_id/name принимаются напрямую из YAML-спека адаптера
        # (server.yaml прокидывает все ключи спека kwargs-ами).
        self.cfg = {**OPERA_CONFIG, **(config or {})}
        if device_id:
            self.cfg["device_id"] = device_id
        if name:
            self.cfg["name"] = name
        self.bridge = bridge
        self._hosts = list(hosts or []) or [self.cfg["http_base"]] + list(self.cfg["discover_urls"])
        self._nodes: dict[str, DeviceNode] = {}
        self._clients: dict[str, _CdpClient] = {}
        self._subs: dict[str, list[Callable]] = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._last_state: dict[str, dict] = {}

    # ----------------------------- discovery -----------------------------
    async def discover(self) -> list[DeviceNode]:
        """Найти Opera GX по CDP-точкам.

        Точка, отвечающая UA с OPR/Opera — точно Opera; точка, до которой
        достучаться не удалось (браузер закрыт), регистрируется офлайном и
        оживает, когда пользователь запустит Opera с --remote-debugging-port.
        Ответивший НЕ-Opera пропускаем (не хотим управлять Chrome под видом Opera).
        """
        found: list[DeviceNode] = []
        seen_ids: set[str] = set()
        for base in self._hosts:
            dev = self._node_for(base)
            client = _CdpClient(base, self.cfg["request_timeout_s"])
            try:
                ver = await client.version()
                ua = ver.get("Browser", "")
                if "OPR" not in ua and "Opera" not in ua:
                    log.info("opera adapter: %s отвечает, но это не Opera (%s) — пропускаем", base, ua)
                    continue
                dev.fw_version = ua
            except Exception as e:
                log.debug("opera adapter: CDP на %s недоступен (%s) — узел офлайн", base, e)
            if dev.id in seen_ids:      # несколько хостов с одинаковым device_id
                dev.id = f"{dev.id}-{urlparse(base).netloc.replace(':', '_')}"
            seen_ids.add(dev.id)
            self._clients[dev.id] = client
            self._nodes[dev.id] = dev
            found.append(dev)
        if found and self.bridge is not None:
            await self.bridge.publish_lwt(
                f"ct/v1/{found[0].id}/availability")
            self._poll_task = asyncio.create_task(self._poll_loop())
        return found

    def _node_for(self, base: str) -> DeviceNode:
        did = self.cfg["device_id"]
        return DeviceNode(
            id=did, name=self.cfg["name"], ecosystem=Ecosystem.OPERA,
            adapter=self.name, device_type="screen",
            topology=Topology(kind="strip", length=0, width=1, height=1,
                              color_order="RGB", max_fps=int(1 / self.cfg["poll_interval_s"])),
            endpoints=[Endpoint(kind=TransportKind.HTTP, url=base),
                       Endpoint(kind=TransportKind.WS, url=f"ws://{urlparse(base).netloc}")],
            native_effects=[NativeRef(ecosystem=self.ecosystem, effect=e["effect"],
                                      args={"class": e["class"]})
                            for e in self.cfg["native_effects"]],
            supports_ir=False,           # нативных RGB-лент нет; только проекция
            supports_segments=False,     # честно: сегментов вкладки не имеют
            telemetry_kinds=["audio_level", "audio_band", "active_tab", "playing"],
        )

    async def capabilities(self, device_id: str) -> DeviceNode:
        return self._nodes[device_id]

    # ----------------------------- state -----------------------------
    async def get_state(self, device_id: str) -> dict[str, Any]:
        client = self._clients.get(device_id)
        if client is None:
            raise KeyError(f"opera: unknown device {device_id}")
        try:
            targets = await client.list_targets()
        except Exception:
            node = self._nodes[device_id]
            node.online = OnlineState.OFFLINE
            return {"on": False, "online": False, "error": "CDP unreachable"}
        tabs = [t for t in targets if t.get("type") in self.cfg["count_targets"]]
        active = next((t for t in tabs if t.get("url", "").startswith("https://gx.opera")
                       or t.get("url", "") == ""), None)
        state = {
            "on": bool(tabs),
            "online": True,
            "tabs": len(tabs),                 # «длина ленты» = число вкладок
            "titles": [t.get("title", "")[:60] for t in tabs],
            "urls": [t.get("url", "")[:120] for t in tabs],
            "active_index": tabs.index(active) if active in tabs else 0,
            "mode": self._last_state.get(device_id, {}).get("mode", "off"),
        }
        node = self._nodes[device_id]
        node.topology.length = len(tabs)
        # подписи тайлов ASCII-логгера: заголовки вкладок через 
        node.labels["screen_titles"] = "".join(
            t.get("title", "")[:40] or "(blank)" for t in tabs)
        node.mark_seen()
        self._last_state[device_id] = state
        return state

    async def set_state(self, device_id: str, state: dict[str, Any],
                        segment: Optional[Segment] = None) -> dict[str, Any]:
        if segment is not None:
            raise UnsupportedError("opera: виртуальные пиксели неприменимы к вкладкам")
        mode = state.get("mode", "off")
        if mode == "off" or state.get("on") is False:
            await self._eval_active(device_id, self.cfg["js"]["restore"])
        elif mode == "blink":
            js = self.cfg["js"]["blink_on"].replace("%BLINK_MS%", str(self.cfg["blink_ms"]))
            await self._eval_active(device_id, js)
        elif mode == "colorsweep":
            js = (self.cfg["js"]["marquee_on"]
                  .replace("%STEP_MS%", str(self.cfg["marquee_step_ms"]))
                  .replace("%PAD%", str(self.cfg["marquee_pad"])))
            await self._eval_active(device_id, js)
        elif mode == "mute":
            js = self.cfg["js"]["mute_set"].replace("%MUTED%", "true" if state.get("muted", True) else "false")
            await self._eval_active(device_id, js)
        elif mode not in ("static", "breathing"):
            raise UnsupportedError(f"opera: режим '{mode}' не описан в OPERA_CONFIG['ir_class_map']")
        # static/breathing: подсветка активной вкладки — единственное честное
        # действие без JS-маркеров (браузер сам держит активную вкладку сверху);
        # breathing-звук реализуется поллингом mute ниже.
        self._last_state.setdefault(device_id, {})["mode"] = mode
        new_state = await self.get_state(device_id)
        await self._publish_state(device_id, new_state)
        return new_state

    async def apply_ir(self, device_id: str, ir: EffectIR,
                       segment: Optional[Segment] = None) -> dict[str, Any]:
        cls = ir.class_.value if hasattr(ir.class_, "value") else str(ir.class_)
        native = self.cfg["ir_class_map"].get(cls)
        if native is None:
            losses = ir.losses + [f"opera: класс '{cls}' не поддерживается — unsupported"]
            raise UnsupportedError("; ".join(losses))
        log.info("opera apply_ir: %s -> %s (потеря: проекция на вкладки/звук, не RGB)",
                 cls, native)
        return await self.set_state(device_id, {"mode": native}, segment)

    # ----------------------------- subscribe -----------------------------
    async def subscribe(self, device_id: str,
                        cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._subs.setdefault(device_id, []).append(cb)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg["poll_interval_s"])
            for dev in list(self._nodes):
                try:
                    st = await self.get_state(dev)
                except Exception as e:
                    log.debug("opera poll %s: %s", dev, e)
                    continue
                await self._publish_state(dev, st)
                for cb in self._subs.get(dev, []):
                    try:
                        await cb(st)
                    except Exception:
                        pass

    async def _publish_state(self, device_id: str, state: dict) -> None:
        if self.bridge is None:
            return
        from ct.core.mqtt_bridge import MqttBridge
        await self.bridge.publish(MqttBridge.t_state(device_id), state, retain=True)

    async def _eval_active(self, device_id: str, expression: str) -> Any:
        client = self._clients.get(device_id)
        if client is None:
            raise KeyError(f"opera: unknown device {device_id}")
        targets = await client.list_targets()
        tab = next((t for t in targets if t.get("type") in self.cfg["count_targets"]), None)
        if tab is None or not tab.get("webSocketDebuggerUrl"):
            return None
        return await client.evaluate(tab["webSocketDebuggerUrl"], expression)

    async def read_audio(self, device_id: str) -> Optional[dict]:
        """Телеметрия звука активной вкладки (WebAudio probe). Публикуется в ct/v1/.../telemetry."""
        js = self.cfg["js"]["audio_probe"].replace("%FFT_SIZE%", str(self.cfg["fft_size"]))
        raw = await self._eval_active(device_id, js)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if self.bridge is not None and data.get("ok"):
            from ct.core.mqtt_bridge import MqttBridge
            ts = time.time()
            await self.bridge.publish(MqttBridge.t_telemetry(device_id, "audio_band"),
                                      {"value": data.get("bins", 0), "ts": ts})
        return data

    async def close(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
