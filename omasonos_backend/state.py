from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLUGIN_ID = "io.github.ctl0v0.omasonos"


def default_state_path() -> Path:
    base = Path(
        os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    )
    return base / PLUGIN_ID / "state.json"


@dataclass
class PersistentState:
    selected_room_uid: str = ""
    cached_hosts: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> "PersistentState":
        target = path or default_state_path()
        try:
            raw: Any = json.loads(target.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        hosts = raw.get("cachedHosts", [])
        if not isinstance(hosts, list):
            hosts = []
        return cls(
            selected_room_uid=str(raw.get("selectedRoomUid", "") or ""),
            cached_hosts=sorted({str(host) for host in hosts if host}),
        )

    def save(self, path: Path | None = None) -> None:
        target = path or default_state_path()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.chmod(0o700)
        payload = {
            "selectedRoomUid": self.selected_room_uid,
            "cachedHosts": sorted(set(self.cached_hosts)),
        }
        temp = target.with_suffix(".tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=2) + "\n")
        temp.chmod(0o600)
        os.replace(temp, target)
