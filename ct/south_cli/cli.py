"""Консольная программа Chromatransposer (Rich REPL) — работает локально на ПК.

Режимы:
  ct                     — интерактивная консоль к запущенному серверу (REST)
  ct --local             — встроенный режим: сервер поднимается в этом же
                           процессе (для USB-прожинга без поднятого REST,
                           команды идут напрямую в Server-объект)

Команды консоли см. HELP; история/табы не входят в MVP.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

HELP = """
команды:
  devices                        список устройств (онлайн/экосистема/топология)
  caps <device>                  capabilities узла
  state <device>                 текущее состояние
  set <device> k=v [k=v ...]     команда (on=true bri=128)
  ir <device> <json|@file>       применить EffectIR напрямую (native_ct/generic)
  port <src_json> <dst_device>   портирование native->IR->native
  group create <id> <pat,...> [--sync] [seg:dev=start:len:off,...]
  group run <id> k=v [...]       одна команда — много узлов
  scene save <id> <json|@file> | scene run <id>
  schedule add <id> <scene> "daily HH:MM"
  reconcile                      desired vs reported, правка дрейфа
  telemetry                      последние данные сенсоров (звук и т.п.)
  release <device>               снять админ-захват
  provision window devices|tokens on|off
  provision usb <device_id> <ssid> <wifi_pass>   (только --local)
  token issue <caller> user|admin
  tick start [hz] | tick stop
  help | quit
"""


class CliClient:
    def __init__(self, base_url: str, token: str):
        self.base, self.token = base_url.rstrip("/"), token
        self.h = {"Authorization": f"Bearer {token}"}

    def get(self, path):
        r = requests.get(self.base + path, headers=self.h, timeout=5)
        r.raise_for_status()
        return r.json()

    def post(self, path, body=None):
        r = requests.post(self.base + path, json=body or {}, headers=self.h, timeout=5)
        r.raise_for_status()
        return r.json()


def _kv(pairs):
    out = {}
    for p in pairs:
        k, v = p.split("=", 1)
        try:
            out[k] = json.loads(v)
        except Exception:
            out[k] = v
    return out


def _arg_token(s: str) -> dict:
    if s.startswith("@"):
        with open(s[1:]) as f:
            return json.load(f)
    return json.loads(s)


def render_devices(rows) -> None:
    t = Table(title="Устройства")
    for col in ("id", "экосистема", "адаптер", "топология", "IR", "сегм.", "онлайн"):
        t.add_column(col)
    for d in rows:
        top = d["topology"]
        topo = f"{top['width']}x{top['height']}" if top["kind"] == "matrix" else str(top["length"])
        t.add_row(d["id"], d["ecosystem"], d["adapter"], topo,
                  "yes" if d["supports_ir"] else "-",
                  "yes" if d["supports_segments"] else "-",
                  d["online"])
    console.print(t)


async def handle_line(client: CliClient | None, local_server, parts: list[str]) -> None:
    cmd = parts[0] if parts else ""
    args = parts[1:]

    if cmd == "devices":
        render_devices(client.get("/api/v1/devices") if client
                       else [d.model_dump() for d in local_server.registry.all()])
    elif cmd == "caps" and args:
        if client:
            console.print_json(json=client.get(f"/api/v1/devices/{args[0]}"))
        else:
            node = await local_server._adapter_for(
                local_server.registry.get(args[0])).capabilities(args[0])
            console.print_json(node.model_dump_json())
    elif cmd == "state" and args:
        console.print_json(json=(client.get(f"/api/v1/devices/{args[0]}/state") if client
                                 else await local_server.registry.get(args[0]) and
                                      local_server._adapter_for(local_server.registry.get(args[0]))
                                      .get_state(args[0])))
    elif cmd == "set" and len(args) >= 2:
        body = {"state": _kv(args[1:])}
        res = (client.post(f"/api/v1/devices/{args[0]}/state", body) if client
               else await local_server.orchestrator.command_device(
                   args[0], body["state"], _local_caller(local_server)))
        console.print_json(json=res)
    elif cmd == "ir" and len(args) >= 2:
        ir = _arg_token(args[1])
        body = {"ir": ir}
        res = (client.post(f"/api/v1/devices/{args[0]}/state", body) if client
               else await local_server.orchestrator.command_device(
                   args[0], {}, _local_caller(local_server),
                   __import__("ct.core.ir", fromlist=["EffectIR"]).EffectIR(**ir)))
        console.print_json(json=res)
    elif cmd == "port" and len(args) >= 2:
        src = _arg_token(args[0])
        body = {"source": src, "target_device": args[1]}
        res = (client.post("/api/v1/port", body) if client
               else local_server.port_native(
                   __import__("ct.core.ir", fromlist=["NativeRef"]).NativeRef(**src),
                   args[1]).model_dump())
        console.print_json(json=res)
    elif cmd == "reconcile":
        console.print_json(json=(client.post("/api/v1/reconcile") if client
                                 else await local_server.orchestrator.reconcile()))
    elif cmd == "telemetry":
        console.print_json(json=(client.get("/api/v1/telemetry") if client
                                 else local_server.telemetry_latest))
    elif cmd == "release" and args:
        from ct.core.orchestrator import Caller
        if client:
            client.post(f"/api/v1/devices/{args[0]}/release")
        else:
            local_server.orchestrator.release_capture(args[0], _local_caller(local_server, admin=True))
        console.print("[green]released[/]")
    elif cmd == "tick" and args:
        oc = local_server.orchestrator if not client else None
        if oc is None:
            console.print("[red]tick управляет только --local сервером[/]")
        elif args[0] == "start":
            await oc.start_tick(1.0 / float(args[1] if len(args) > 1 else 5))
            console.print("[green]tick started[/]")
        else:
            await oc.stop_tick()
            console.print("[yellow]tick stopped[/]")
    elif cmd == "provision" and args[:1] == ["usb"]:
        if client:
            console.print("[red]USB-прожинг только в --local[/]")
        else:
            _, dev_id, ssid, wifi_pass = args
            cred = local_server.provisioning.usb_provision(dev_id, ssid, wifi_pass)
            console.print(Panel(f"device_id={cred.device_id}\nshared_key={cred.shared_key}",
                                title="выдано устройству"))
    elif cmd == "provision" and args[:1] == ["window"]:
        _, which, onoff = args
        sig = local_server.provisioning.admin_secret if not client else ""
        body = {"which": which, "opened": onoff == "on", "admin_signature": sig}
        if client:
            console.print_json(json=client.post("/api/v1/provision/windows", body))
        else:
            local_server.provisioning.set_window(which, onoff == "on", sig)
            console.print(f"[green]window {which}: {onoff}[/]")
    elif cmd == "token" and len(args) >= 2:
        caller_id, role = args[0], args[1]
        if client:
            console.print("[red]токены выдаёт только локальная раздача[/]")
        else:
            sig = local_server.provisioning.admin_secret
            tok = local_server.provisioning.issue_token(caller_id, role, sig)
            console.print(Panel(tok.model_dump_json(indent=2), title="API токен"))
    elif cmd in ("group", "scene", "schedule"):
        console.print("[yellow]заглушка v0.2: используйте REST /api/v1/groups|scenes[/]")
    else:
        console.print(HELP)


def _local_caller(server, admin: bool = True):
    from ct.core.orchestrator import Caller
    return Caller(id="cli-local", role="admin" if admin else "user")


async def repl_loop(client, local_server) -> None:
    console.print("[bold cyan]Chromatransposer CLI[/] (help — список команд)")
    while True:
        try:
            line = console.input("[bold green]ct>[/] ")
        except (EOFError, KeyboardInterrupt):
            break
        parts = shlex.split(line)
        if not parts:
            continue
        if parts[0] in ("quit", "exit"):
            break
        try:
            await handle_line(client, local_server, parts)
        except requests.HTTPError as e:
            console.print(f"[red]{e.response.status_code}: {e.response.text}[/]")
        except Exception as e:
            console.print(f"[red]{type(e).__name__}: {e}[/]")


def main() -> None:
    ap = argparse.ArgumentParser(prog="ct")
    ap.add_argument("--url", default="http://127.0.0.1:8300")
    ap.add_argument("--token", default="")
    ap.add_argument("--local", action="store_true",
                    help="поднять сервер в этом процессе (без сети)")
    ap.add_argument("--config", default="configs/server.yaml")
    ns = ap.parse_args()

    if ns.local:
        from ct.core.server import Server

        async def _run():
            srv = Server(ns.config)
            try:
                await srv.start()
            except Exception as e:
                console.print(f"[yellow]MQTT недоступен ({e}); работаю с реестром из конфига[/]")
            await repl_loop(None, srv)
            await srv.stop()
        asyncio.run(_run())
    else:
        client = CliClient(ns.url, ns.token)
        asyncio.run(repl_loop(client, None))


if __name__ == "__main__":
    main()
