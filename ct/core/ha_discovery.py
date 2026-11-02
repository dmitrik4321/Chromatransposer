"""Discovery-экспорт канонических устройств Chromatransposer в Home Assistant.

HA умеет автоматически подхватывать сущности из MQTT (discovery-протокол:
топики homeassistant/<component>/<node_id>/<field> с retain). Экспортируем:
  * light (json schema) на каждое устройство — on/bri/color/effect;
  * sensor на каждую телеметрию (например audio_level у устройства с микрофоном);
  * scene на каждую сцену прослойки (через service-вызовы не идём — HA-side
    scene собирается пользователем поверх наших lights).

Исходники HA не трогаем: это штатный mqtt-discovery, который HA понимает сам.
Компонент command/state публикуем в канон ct/v1/* — там уже всё есть.

Всё меняемое — DISCOVERY_CONFIG; YAML-секция homeassistant.discovery поверх.
"""
from __future__ import annotations

from typing import Optional

# ------------------------------ КОНФИГ DISCOVERY -------------------------------
DISCOVERY_CONFIG = {
    "enabled": True,
    "prefix": "homeassistant",        # стандартный префикс HA MQTT-discovery
    "node_prefix": "chromatransposer",# uid-часть имени: <prefix>/light/<node_prefix>-<dev>/...
    "origin_name": "Chromatransposer",
    "unique_base": "ct",              # префикс unique_id сущностей
}
# -----------------------------------------------------------------------------


class HaDiscoveryExporter:
    """Пишет retained discovery-конфиги HA в тот же локальный брокер."""

    def __init__(self, bridge, registry, cfg: Optional[dict] = None):
        self.bridge = bridge          # MqttBridge (уже подключён к брокеру)
        self.registry = registry
        self.cfg = {**DISCOVERY_CONFIG, **(cfg or {})}
        self._published: set[str] = set()

    def _topic(self, component: str, node: str, field: str) -> str:
        # homeassistant/<component>/<node_prefix>-<node>/<field>
        return (f"{self.cfg['prefix']}/{component}/"
                f"{self.cfg['node_prefix']}-{node}/{field}")

    async def publish_device(self, node) -> None:
        """Light-сущность для одного канонического узла."""
        if not self.cfg.get("enabled", True):
            return
        dev = node.id
        uniq = f"{self.cfg['unique_base']}_{dev}"
        base = self._topic("light", dev, "")
        avail = self.bridge.t_avail(dev)
        state_t = self.bridge.t_state(dev)
        set_t = self.bridge.t_set(dev)
        payload = {
            "name": node.name or dev,
            "unique_id": uniq,
            "availability_topic": avail,
            "payload_available": "online",
            "payload_not_available": "offline",
            "schema": "json",
            "state_topic": state_t,
            "command_topic": set_t,
            "command_on_template": '{"on": true}',
            "command_off_template": '{"on": false}',
            "state_value_template": "{{ value_json.on }}",
            "brightness": True,
            "brightness_value_template": "{{ value_json.bri }}",
            "brightness_command_template": '{"bri": {{ value }}}',
            "color_temp": False,
            "rgb": True,
            "rgb_value_template": (
                "{% if value_json.color is defined %}"
                "{{ value_json.color.r }},{{ value_json.color.g }},{{ value_json.color.b }}"
                "{% endif %}"),
            "effect": True,
            "effect_list": sorted({e.effect for e in node.native_effects}) or ["none"],
            "effect_value_template": "{{ value_json.effect | default('none') }}",
            "effect_command_template": '{"effect": "{{ value }}"}',
            "device": {
                "identifiers": [uniq],
                "name": node.name or dev,
                "manufacturer": "Chromatransposer",
                "model": f"{node.ecosystem.value} ({node.adapter})",
                "sw_version": node.fw_version or "",
            },
            "origin": {"name": self.cfg["origin_name"]},
        }
        await self.bridge.publish(base + "config", payload, retain=True)
        self._published.add(base + "config")

    async def publish_telemetry_sensors(self, device_id: str, kinds: list[str]) -> None:
        """Sensor-сущности на каждый вид телеметрии (звук и т.п.)."""
        if not self.cfg.get("enabled", True):
            return
        for kind in kinds:
            uniq = f"{self.cfg['unique_base']}_{device_id}_{kind}"
            topic = self._topic("sensor", f"{device_id}_{kind}", "config")
            payload = {
                "name": f"{device_id} {kind}",
                "unique_id": uniq,
                "state_topic": self.bridge.t_telemetry(device_id, kind),
                "value_template": "{{ value_json.value }}",
                "device": {"identifiers": [f"{self.cfg['unique_base']}_{device_id}"]},
                "origin": {"name": self.cfg["origin_name"]},
            }
            await self.bridge.publish(topic, payload, retain=True)
            self._published.add(topic)

    async def publish_all(self) -> int:
        n = 0
        for node in self.registry.all():
            await self.publish_device(node)
            if node.telemetry_kinds:
                await self.publish_telemetry_sensors(node.id, node.telemetry_kinds)
            n += 1
        return n

    async def remove_device(self, device_id: str) -> None:
        """Снять discovery-конфиг (retain-пустышка) при удалении узла."""
        topic = self._topic("light", device_id, "config")
        await self.bridge.publish(topic, {}, retain=True)
