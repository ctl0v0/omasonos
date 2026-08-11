from __future__ import annotations

import json
import logging
import select
import sys
import time
from collections.abc import Callable
from typing import Any, TextIO

from .controller import ControllerError, SonosController

LOG = logging.getLogger(__name__)


class ProtocolServer:
    def __init__(self, controller: SonosController) -> None:
        self.controller = controller
        self.panel_open = False
        self.last_refresh = 0.0

    def _emit(self, payload: dict[str, Any], output: TextIO) -> None:
        output.write(json.dumps(payload, separators=(",", ":")) + "\n")
        output.flush()

    def emit_snapshot(self, output: TextIO) -> None:
        try:
            snapshot = self.controller.refresh()
        except Exception as exc:  # noqa: BLE001 - keep the service alive on LAN faults
            LOG.exception("Sonos refresh failed")
            snapshot = {
                "type": "snapshot",
                "version": 1,
                "status": {
                    "state": "error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "lastRefreshEpochMs": int(time.time() * 1000),
                },
                "selectedAnchorRoomUid": "",
                "targetGroupUid": "",
                "households": [],
                "target": None,
                "playback": {
                    "state": "STOPPED",
                    "title": "",
                    "artist": "",
                    "album": "",
                    "artworkUrl": "",
                    "source": "UNKNOWN",
                    "positionSec": None,
                    "durationSec": None,
                    "availableActions": [],
                },
            }
        self.last_refresh = time.monotonic()
        self._emit(snapshot, output)

    def handle(self, request: dict[str, Any], output: TextIO) -> None:
        request_id = str(request.get("id", "") or "")
        op = str(request.get("op", "") or "")
        # PLAN.md defines operation parameters at the top level. Accept a
        # nested `args` object too for forward/backward compatibility, with
        # explicit top-level fields winning when both are supplied.
        nested_args = request.get("args") or {}
        if not isinstance(nested_args, dict):
            nested_args = {}
        args = dict(nested_args)
        for key, value in request.items():
            if key not in {"id", "op", "args"}:
                args[key] = value

        if op == "setPanelOpen":
            self.panel_open = bool(args.get("open", False))
            self._emit({"type": "result", "id": request_id, "ok": True}, output)
            return

        if op == "refresh":
            self._emit({"type": "result", "id": request_id, "ok": True}, output)
            self.emit_snapshot(output)
            return

        # Keep dispatch lazy: building a table of bound methods up front makes
        # even an unrelated command depend on every optional controller method.
        dispatch: dict[str, Callable[[], None]] = {
            "playPause": lambda: self.controller.play_pause(),
            "play": lambda: self.controller.play(),
            "pause": lambda: self.controller.pause(),
            "next": lambda: self.controller.next(),
            "previous": lambda: self.controller.previous(),
            "seek": lambda: self.controller.seek(args.get("positionSec", 0)),
            "selectGroup": lambda: self.controller.select_group(
                str(args.get("groupUid", ""))
            ),
            "setGroupVolume": lambda: self.controller.set_group_volume(
                args.get("volume", 0)
            ),
            "adjustGroupVolume": lambda: self.controller.adjust_group_volume(
                args.get("delta", 0)
            ),
            "setGroupMute": lambda: self.controller.set_group_mute(
                args.get("mute", False)
            ),
            "setRoomVolume": lambda: self.controller.set_room_volume(
                str(args.get("roomUid", "")), args.get("volume", 0)
            ),
            "adjustRoomVolume": lambda: self.controller.adjust_room_volume(
                str(args.get("roomUid", "")), args.get("delta", 0)
            ),
            "setRoomMute": lambda: self.controller.set_room_mute(
                str(args.get("roomUid", "")), args.get("mute", False)
            ),
            "applyMembers": lambda: self.controller.apply_members(
                [str(uid) for uid in args.get("roomUids", [])]
            ),
        }

        action = dispatch.get(op)
        if action is None:
            self._emit(
                {
                    "type": "result",
                    "id": request_id,
                    "ok": False,
                    "error": f"Unknown operation: {op}",
                },
                output,
            )
            return

        try:
            action()
            self._emit({"type": "result", "id": request_id, "ok": True}, output)
        except (ControllerError, ValueError, TypeError, OSError) as exc:
            self._emit(
                {
                    "type": "result",
                    "id": request_id,
                    "ok": False,
                    "error": str(exc),
                },
                output,
            )
        except Exception as exc:  # noqa: BLE001 - preserve long-running service
            LOG.exception("Unhandled Sonos command error")
            self._emit(
                {
                    "type": "result",
                    "id": request_id,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                output,
            )
        finally:
            # Mutations are followed by authoritative state, including partial
            # failures, so the QML never has to pretend its optimistic view won.
            self.emit_snapshot(output)

    def serve(self, input_stream: TextIO = sys.stdin, output: TextIO = sys.stdout) -> None:
        self.emit_snapshot(output)
        while True:
            interval = 2.0 if self.panel_open else 10.0
            timeout = max(0.0, interval - (time.monotonic() - self.last_refresh))
            readable, _, _ = select.select([input_stream], [], [], timeout)
            if not readable:
                self.emit_snapshot(output)
                continue
            line = input_stream.readline()
            if line == "":
                return
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                self._emit(
                    {
                        "type": "result",
                        "id": "",
                        "ok": False,
                        "error": f"Invalid JSON: {exc.msg}",
                    },
                    output,
                )
                continue
            if not isinstance(request, dict):
                self._emit(
                    {
                        "type": "result",
                        "id": "",
                        "ok": False,
                        "error": "Protocol message must be a JSON object",
                    },
                    output,
                )
                continue
            self.handle(request, output)
