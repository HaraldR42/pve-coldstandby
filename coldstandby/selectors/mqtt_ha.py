"""`MqttHaSelector` -- online lab selector over MQTT, with Home Assistant discovery.

This is the online selector `build_selectors` wires in by default; it takes
the place of the REST `HomeAssistantSelector` whenever `mqtt_broker` is
configured. It needs the ``mqtt`` extra::

    apt install python3-paho-mqtt

Topic tree -- everything under ``<mqtt_base_topic>/<node>``:

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
online                yes      "true" while the node is up. `MqttPresence`
                               publishes it on start and, on a clean stop,
                               "false"; if the process just vanishes the
                               broker publishes "false" via the LWT.
===================== ======== =================================================

When ``mqtt_broker`` is set the controller does not exit after the boot
work: it holds an `MqttPresence` connection open (marking ``online``) until
systemd stops it. See main.py.

Home Assistant MQTT **device** discovery is published (once per boot, before
the last_boot_* values) to
``<discovery_prefix>/device/<node-id>/config`` -- one device carrying a
``select`` (replication / lab only; Emergency is never remotely selectable),
a ``sensor`` per last_boot_* value, and an ``online`` connectivity
``binary_sensor``. The components carry **no availability topic on
purpose**: the select must stay operable while the backup node -- the very
thing that publishes this -- is powered off, which is exactly when you set
it. (``online`` is a plain binary_sensor, *not* wired as availability, for
the same reason.)
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from collections.abc import Generator
from typing import TYPE_CHECKING

from ..config import PROJECT_NAME, Config
from ..mode import Mode, ModeDecision, ModeSelector, ModeSelectorUnavailable

if TYPE_CHECKING:
    # For the type checker paho is always present, so `mqtt.Client` and
    # `CallbackAPIVersion` resolve. At runtime it is the one optional
    # dependency: absent, `mqtt` is None and every code path that needs it
    # goes through `_require_paho()` first.
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
else:
    try:
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


def _build_client(cfg: Config) -> mqtt.Client:
    client = mqtt.Client(
        CallbackAPIVersion.VERSION2,
        client_id=f"{PROJECT_NAME}-{cfg.node}-{os.getpid()}",
    )
    if cfg.mqtt_username:
        client.username_pw_set(cfg.mqtt_username, cfg.mqtt_password or None)
    if cfg.mqtt_tls:
        client.tls_set(ca_certs=cfg.mqtt_tls_ca_cert or None)
    return client


def _reason_is_failure(reason_code) -> bool:
    return bool(getattr(reason_code, "is_failure", reason_code))


class MqttHaSelector(ModeSelector):
    def __init__(self, cfg: Config):
        self._cfg = cfg

    # -- ModeSelector ------------------------------------------------

    def mode_requested(self) -> Mode | None:
        topic = self._topic("next_boot_mode")
        log.info("Checking MQTT %s ...", topic)
        payload = self._read_retained(topic)
        if payload is None:
            log.info("MQTT %s not set -- no request.", topic)
            return None
        value = payload.strip().lower()
        if value == self._cfg.ha_lab_option:
            log.info("MQTT %s = %r -> requesting Lab mode.", topic, value)
            return Mode.LAB
        if value in ("", self._cfg.ha_replication_option):
            log.info("MQTT %s = %r -> no request (default).", topic, value)
            return None
        log.warning(
            "MQTT %s = %r is not an option offered here -- ignoring.", topic, value
        )
        return None

    def clear(self) -> None:
        """Consume the request: reset next_boot_mode to the replication
        option (retained), so the next unattended boot is normal again."""
        log.info(
            "Resetting MQTT %s to %r (request consumed).",
            self._topic("next_boot_mode"), self._cfg.ha_replication_option,
        )
        with self._connection() as client:
            self._publish(client, self._topic("next_boot_mode"),
                          self._cfg.ha_replication_option, retain=True)

    def publish_result(self, decision: ModeDecision) -> None:
        d = decision.as_dict()
        log.info(
            "Publishing boot result to MQTT under %s (mode=%s, decided_by=%s).",
            self._cfg.mqtt_topic_base, d["mode"], d["decided_by"] or "default",
        )
        with self._connection() as client:
            if self._cfg.mqtt_discovery:
                log.info(
                    "Publishing Home Assistant discovery for device %r to %s.",
                    self._node_id(), self._discovery_topic(),
                )
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
        log.info("MQTT boot result published (%d topics).", len(_LAST_BOOT_KEYS) + 1)

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
            if not received.wait(self._cfg.mqtt_timeout_seconds):
                log.debug(
                    "No retained message on %s within %.0fs.",
                    topic, self._cfg.mqtt_timeout_seconds,
                )
        return got.get("payload")

    def _publish(self, client: mqtt.Client, topic: str, payload: str, *, retain: bool) -> None:
        log.debug("MQTT publish %s (retain=%s, %d bytes).", topic, retain, len(payload))
        info = client.publish(topic, payload, qos=1, retain=retain)
        info.wait_for_publish(self._cfg.mqtt_timeout_seconds)
        if not info.is_published():
            raise ModeSelectorUnavailable(f"MQTT publish to {topic} not confirmed")

    @contextlib.contextmanager
    def _connection(self) -> Generator[mqtt.Client, None, None]:
        self._require_paho()
        client = self._make_client()
        target = f"{self._cfg.mqtt_broker}:{self._cfg.mqtt_port}"
        log.debug(
            "Connecting to MQTT %s (tls=%s, auth=%s) ...",
            target, self._cfg.mqtt_tls, bool(self._cfg.mqtt_username),
        )
        try:
            client.connect(self._cfg.mqtt_broker, self._cfg.mqtt_port, keepalive=60)
        except OSError as exc:
            raise ModeSelectorUnavailable(
                f"MQTT connect to {target} failed: {exc}"
            ) from exc
        client.loop_start()
        log.debug("MQTT %s connected.", target)
        try:
            yield client
        finally:
            with contextlib.suppress(Exception):
                client.loop_stop()
                client.disconnect()
            log.debug("MQTT %s disconnected.", target)

    def _make_client(self) -> mqtt.Client:
        return _build_client(self._cfg)

    @staticmethod
    def _require_paho() -> None:
        if mqtt is None:
            raise ModeSelectorUnavailable(
                "paho-mqtt is not installed -- apt install python3-paho-mqtt"
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
                "online": {
                    "p": "binary_sensor",
                    "name": "Online",
                    "state_topic": f"{base}/online",
                    "unique_id": f"{node_id}_online",
                    "object_id": f"{node_id}_online",
                    "device_class": "connectivity",
                    "payload_on": "true",
                    "payload_off": "false",
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


class MqttPresence:
    """Holds an MQTT connection that marks this node ``online`` for as long
    as the process runs.

    The connection carries a Last Will and Testament, so if the process
    just vanishes -- crash, ``kill -9``, power cut -- the broker publishes
    ``online = "false"`` (retained) on our behalf. On a clean ``stop()`` we
    publish it ourselves first. ``connect_async`` + a background loop means
    a broker that is briefly down at boot (or restarts later) is retried
    automatically; ``online = "true"`` is re-published on every reconnect.
    """

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._topic = f"{cfg.mqtt_topic_base}/online"
        self._client: mqtt.Client | None = None

    @property
    def active(self) -> bool:
        return self._client is not None

    def start(self) -> None:
        if mqtt is None:
            log.warning("paho-mqtt not installed -- no MQTT online presence.")
            return
        client = _build_client(self._cfg)
        client.will_set(self._topic, "false", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        try:
            client.connect_async(self._cfg.mqtt_broker, self._cfg.mqtt_port, keepalive=60)
        except (OSError, ValueError) as exc:
            log.warning("MQTT online presence: %s -- no presence held.", exc)
            return
        client.loop_start()
        self._client = client
        log.info(
            "MQTT online presence: connecting to %s:%d in the background.",
            self._cfg.mqtt_broker, self._cfg.mqtt_port,
        )

    def stop(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        log.info("MQTT: marking offline at %s and disconnecting.", self._topic)
        with contextlib.suppress(Exception):
            info = client.publish(self._topic, "false", qos=1, retain=True)
            info.wait_for_publish(self._cfg.mqtt_timeout_seconds)
        with contextlib.suppress(Exception):
            client.loop_stop()
            client.disconnect()

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if _reason_is_failure(reason_code):
            log.warning("MQTT online presence: broker refused connection (%s).", reason_code)
            return
        client.publish(self._topic, "true", qos=1, retain=True)
        log.info("MQTT: marked online at %s.", self._topic)

    def _on_disconnect(self, *_args) -> None:
        log.info("MQTT online presence: disconnected from broker (will retry).")
