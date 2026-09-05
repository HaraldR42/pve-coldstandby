"""`MqttHaSelector` -- online lab selector over MQTT, with Home Assistant discovery.

This is the online selector `build_selectors` wires in by default; it takes
the place of the REST `HomeAssistantSelector` whenever `mqtt_broker` is
configured. It needs the ``mqtt`` extra::

    pip install pve-coldstandby[mqtt]

Topic tree -- everything under ``<project>/<node>`` (override with
``mqtt_base_topic``):

===================== ======== =================================================
topic                 retained meaning
===================== ======== =================================================
next_boot_mode        yes      requested mode; also the HA select's state AND
                               command topic. Read on boot, then reset to the
                               replication option.
last_boot_mode        yes      \\
last_boot_decided_by  yes      |  the resolved `ModeDecision`, one value per
last_boot_host        yes      |  topic, published on every non-dry boot
last_boot_at          yes      |
last_boot_selectors   yes      /  (JSON: selector class name -> contribution)
===================== ======== =================================================

Home Assistant MQTT **device** discovery is published (once per boot, before
the last_boot_* values) to
``<discovery_prefix>/device/<node-id>/config`` -- one device carrying a
``select`` (replication / lab only; Emergency is never remotely selectable)
and a ``sensor`` per last_boot_* value. The components carry **no
availability topic on purpose**: the select must stay operable while the
backup node -- the very thing that publishes this -- is powered off, which
is exactly when you set it.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from typing import Iterator

from ..config import PROJECT_NAME, Config
from ..mode import Mode, ModeDecision, ModeSelector, ModeSelectorUnavailable

try:  # the one optional dependency in the project
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:  # pragma: no cover - exercised via _require_paho
    mqtt = None
    CallbackAPIVersion = None

log = logging.getLogger(__name__)

_REPO_URL = "https://github.com/HaraldR42/pve-coldstandby"
_LAST_BOOT_KEYS = (
    "last_boot_mode",
    "last_boot_decided_by",
    "last_boot_host",
    "last_boot_at",
    "last_boot_selectors",
)


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("pve-coldstandby")
    except Exception:  # noqa: BLE001 - version string is cosmetic
        return "0.0.0"


class MqttHaSelector(ModeSelector):
    def __init__(self, cfg: Config):
        self._cfg = cfg

    # -- ModeSelector ------------------------------------------------

    def mode_requested(self) -> Mode | None:
        payload = self._read_retained(self._topic("next_boot_mode"))
        if payload is None:
            return None
        value = payload.strip().lower()
        if value == self._cfg.ha_lab_option:
            return Mode.LAB
        if value in ("", self._cfg.ha_replication_option):
            return None
        log.warning(
            "MQTT next_boot_mode=%r is not an option offered here -- ignoring.",
            value,
        )
        return None

    def clear(self) -> None:
        """Consume the request: reset next_boot_mode to the replication
        option (retained), so the next unattended boot is normal again."""
        with self._connection() as client:
            self._publish(client, self._topic("next_boot_mode"),
                          self._cfg.ha_replication_option, retain=True)

    def publish_result(self, decision: ModeDecision) -> None:
        d = decision.as_dict()
        with self._connection() as client:
            if self._cfg.mqtt_discovery:
                self._publish(
                    client, self._discovery_topic(),
                    json.dumps(self._discovery_payload()), retain=True,
                )
            values = {
                "last_boot_mode": d["mode"],
                "last_boot_decided_by": d["decided_by"] or "default",
                "last_boot_host": d["host"],
                "last_boot_at": d["resolved_at"],
                "last_boot_selectors": json.dumps(d["selectors"]),
            }
            for key in _LAST_BOOT_KEYS:
                self._publish(client, self._topic(key), values[key], retain=True)
            # Whatever it held, the request has now been acted on.
            self._publish(client, self._topic("next_boot_mode"),
                          self._cfg.ha_replication_option, retain=True)

    # -- MQTT plumbing ---------------------------------------------

    def _topic(self, key: str) -> str:
        return f"{self._cfg.mqtt_topic_base}/{key}"

    def _read_retained(self, topic: str) -> str | None:
        got: dict[str, str] = {}
        received = threading.Event()

        def on_message(_client, _userdata, message) -> None:
            got["payload"] = message.payload.decode("utf-8", "replace")
            received.set()

        with self._connection() as client:
            client.on_message = on_message
            client.subscribe(topic, qos=1)
            received.wait(self._cfg.mqtt_timeout_seconds)
        return got.get("payload")

    def _publish(self, client, topic: str, payload: str, *, retain: bool) -> None:
        info = client.publish(topic, payload, qos=1, retain=retain)
        info.wait_for_publish(self._cfg.mqtt_timeout_seconds)
        if not info.is_published():
            raise ModeSelectorUnavailable(f"MQTT publish to {topic} not confirmed")

    @contextlib.contextmanager
    def _connection(self) -> Iterator["mqtt.Client"]:
        self._require_paho()
        client = self._make_client()
        try:
            client.connect(self._cfg.mqtt_broker, self._cfg.mqtt_port, keepalive=60)
        except OSError as exc:
            raise ModeSelectorUnavailable(
                f"MQTT connect to {self._cfg.mqtt_broker}:{self._cfg.mqtt_port} failed: {exc}"
            ) from exc
        client.loop_start()
        try:
            yield client
        finally:
            with contextlib.suppress(Exception):
                client.loop_stop()
                client.disconnect()

    def _make_client(self):
        client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=f"{PROJECT_NAME}-{self._cfg.node}-{os.getpid()}",
        )
        if self._cfg.mqtt_username:
            client.username_pw_set(self._cfg.mqtt_username, self._cfg.mqtt_password or None)
        if self._cfg.mqtt_tls:
            client.tls_set(ca_certs=self._cfg.mqtt_tls_ca_cert or None)
        return client

    @staticmethod
    def _require_paho() -> None:
        if mqtt is None:
            raise ModeSelectorUnavailable(
                "paho-mqtt is not installed -- pip install pve-coldstandby[mqtt]"
            )

    # -- Home Assistant discovery --------------------------------

    def _node_id(self) -> str:
        raw = f"{PROJECT_NAME}_{self._cfg.node}"
        return "".join(c if c.isalnum() else "_" for c in raw)

    def _discovery_topic(self) -> str:
        return f"{self._cfg.mqtt_discovery_prefix}/device/{self._node_id()}/config"

    def _discovery_payload(self) -> dict:
        base = self._cfg.mqtt_topic_base
        next_topic = f"{base}/next_boot_mode"
        node_id = self._node_id()

        def sensor(key: str, name: str, **extra) -> dict:
            return {
                "p": "sensor",
                "name": name,
                "state_topic": f"{base}/{key}",
                "unique_id": f"{node_id}_{key}",
                "object_id": f"{node_id}_{key}",
                **extra,
            }

        return {
            "dev": {
                "ids": [node_id],
                "name": f"Cold standby ({self._cfg.node})",
                "mf": PROJECT_NAME,
                "mdl": "backup Proxmox VE node",
                "sw": _version(),
                "cu": _REPO_URL,
            },
            "o": {"name": PROJECT_NAME, "sw": _version(), "url": _REPO_URL},
            "cmps": {
                "next_boot_mode": {
                    "p": "select",
                    "name": "Next boot mode",
                    "unique_id": f"{node_id}_next_boot_mode",
                    "object_id": f"{node_id}_next_boot_mode",
                    "command_topic": next_topic,
                    "state_topic": next_topic,
                    "options": [
                        self._cfg.ha_replication_option,
                        self._cfg.ha_lab_option,
                    ],
                    "retain": True,
                    "icon": "mdi:restart",
                },
                "last_boot_mode": sensor("last_boot_mode", "Last boot mode", icon="mdi:cog"),
                "last_boot_decided_by": sensor("last_boot_decided_by", "Last boot decided by"),
                "last_boot_host": sensor("last_boot_host", "Last boot host"),
                "last_boot_at": sensor("last_boot_at", "Last boot at", device_class="timestamp"),
                "last_boot_selectors": sensor(
                    "last_boot_selectors", "Last boot selectors", icon="mdi:format-list-bulleted"
                ),
            },
            # No "avty"/"availability" key -- see the module docstring.
        }
