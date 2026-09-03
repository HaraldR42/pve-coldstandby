"""`HomeAssistantSelector` -- the online mode selector, backed by Home Assistant.

An optional convenience: a switch a human flips (from their phone, say) to
ask that the *next* boot come up in Lab mode without walking over to plug
in a dongle. By design this selector only ever requests Lab -- Emergency is
never reachable over the network -- and it self-consumes, so a forgotten
flag can't re-trigger Lab on the following unattended boot.

It also implements `publish_result`: after every resolution it writes the
outcome to a Home Assistant entity (``ha_status_entity``) so "what did the
last boot decide, and why" is visible without reading the journal. That is
a one-way status readout, not a control surface.

Backed by a Home Assistant ``input_select`` (and a status entity) over the
REST API. Nothing about it is special -- it's just one module in this
package; see the package docstring for how to add another. Leaving
``ha_base_url`` / ``ha_token`` empty drops this selector entirely.

Deliberately stdlib-only (urllib) so this has zero third-party
dependencies to keep patched.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from ..config import Config
from ..mode import Mode, ModeDecision, ModeSelector, ModeSelectorUnavailable


class HomeAssistantSelector(ModeSelector):
    def __init__(self, cfg: Config):
        self._cfg = cfg

    def mode_requested(self) -> Mode | None:
        if self._state() == self._cfg.ha_lab_option:
            return Mode.LAB
        return None

    def clear(self) -> None:
        self._request(
            "POST",
            "/api/services/input_select/select_option",
            {
                "entity_id": self._cfg.ha_lab_select_entity,
                "option": self._cfg.ha_replication_option,
            },
        )

    def publish_result(self, decision: ModeDecision) -> None:
        entity = self._cfg.ha_status_entity
        if not entity:
            return
        payload = decision.as_dict()
        self._request(
            "POST",
            f"/api/states/{entity}",
            {
                "state": payload["mode"],
                "attributes": {
                    "friendly_name": "Cold-standby last boot",
                    "icon": "mdi:server",
                    "decided_by": payload["decided_by"],
                    "host": payload["host"],
                    "resolved_at": payload["resolved_at"],
                    "selectors": payload["selectors"],
                },
            },
        )

    # -- internals ----------------------------------------------------

    def _state(self) -> str:
        result = self._request(
            "GET", f"/api/states/{self._cfg.ha_lab_select_entity}"
        )
        try:
            return result["state"]
        except (KeyError, TypeError) as exc:
            raise ModeSelectorUnavailable(
                f"unexpected response for {self._cfg.ha_lab_select_entity}: {result!r}"
            ) from exc

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{self._cfg.ha_base_url.rstrip('/')}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._cfg.ha_token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._cfg.ha_timeout_seconds) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            raise ModeSelectorUnavailable(f"{method} {url} failed: {exc}") from exc
