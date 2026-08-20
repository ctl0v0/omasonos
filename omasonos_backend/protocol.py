from __future__ import annotations

import copy
import json
import logging
import select
import sys
import time
from collections.abc import Callable
from typing import Any, TextIO

from .controller import ControllerError, SonosController
from .live_updates import EventSubscriptionManager, WakeQueue

LOG = logging.getLogger(__name__)
EVENT_BURST_SEC = 0.075
EVENT_PANEL_POLL_SEC = 5.0
EVENT_BACKGROUND_POLL_SEC = 15.0
FALLBACK_PANEL_POLL_SEC = 2.0
FALLBACK_BACKGROUND_POLL_SEC = 5.0


class ProtocolServer:
    def __init__(self, controller: SonosController) -> None:
        self.controller = controller
        self.panel_open = False
        self.last_refresh = 0.0
        self.last_snapshot: dict[str, Any] | None = None
        self.event_queue = WakeQueue()
        self.event_subscriptions = EventSubscriptionManager(self.event_queue)

    def _emit(self, payload: dict[str, Any], output: TextIO) -> None:
        output.write(json.dumps(payload, separators=(",", ":")) + "\n")
        output.flush()

    def emit_snapshot(self, output: TextIO, *, rediscover: bool = True) -> None:
        try:
            snapshot = self.controller.refresh(rediscover=rediscover)
            diagnostics = self.event_subscriptions.reconcile(
                self.controller.event_services()
            )
            snapshot.setdefault("status", {})["liveUpdates"] = diagnostics
            self.last_snapshot = copy.deepcopy(snapshot)
        except Exception as exc:  # noqa: BLE001 - keep the service alive on LAN faults
            LOG.exception("Sonos refresh failed")
            if self.last_snapshot is not None:
                snapshot = copy.deepcopy(self.last_snapshot)
                status = snapshot.setdefault("status", {})
                status["state"] = (
                    "ready" if status.get("state") == "ready" else "error"
                )
                status["message"] = f"{type(exc).__name__}: {exc}"
                status["degraded"] = True
                status["lastRefreshEpochMs"] = int(time.time() * 1000)
                snapshot.setdefault("playback", {})["stale"] = True
                snapshot["playback"]["metadataState"] = "cached"
            else:
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
                    "favorites": {
                        "state": "not_loaded",
                        "items": [],
                        "total": 0,
                        "unsupported": 0,
                        "error": "",
                    },
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
                        "metadataState": "empty",
                        "stale": False,
                    },
                }
        self.last_refresh = time.monotonic()
        self._emit(snapshot, output)

    def handle(self, request: dict[str, Any], output: TextIO) -> None:
        request_id = str(request.get("id", "") or "")
        op = str(request.get("op", "") or "")
        args: dict[str, Any] = {}
        for key, value in request.items():
            if key not in {"id", "op"}:
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
            "playFavorite": lambda: self.controller.play_favorite(
                str(args.get("favoriteId", ""))
            ),
            "refreshFavorites": lambda: self.controller.refresh_favorites(),
            "movePlaybackToRoom": lambda: self.controller.move_playback_to_room(
                str(args.get("roomUid", ""))
            ),
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
            LOG.warning("Sonos command %s failed: %s", op, exc)
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
            self.emit_snapshot(output, rediscover=False)

    def serve(self, input_stream: TextIO = sys.stdin, output: TextIO = sys.stdout) -> None:
        pending_event_at: float | None = None
        pending_topology_households: set[str] = set()
        try:
            self.emit_snapshot(output)
            while True:
                live = self.event_subscriptions.complete
                interval = (
                    EVENT_PANEL_POLL_SEC
                    if self.panel_open
                    else EVENT_BACKGROUND_POLL_SEC
                ) if live else (
                    FALLBACK_PANEL_POLL_SEC
                    if self.panel_open
                    else FALLBACK_BACKGROUND_POLL_SEC
                )
                now = time.monotonic()
                poll_timeout = max(0.0, interval - (now - self.last_refresh))
                if pending_event_at is not None:
                    poll_timeout = min(poll_timeout, max(0.0, pending_event_at - now))
                readable, _, _ = select.select(
                    [input_stream, self.event_queue.read_fd], [], [], poll_timeout
                )
                now = time.monotonic()
                if self.event_queue.read_fd in readable:
                    for event in self.event_queue.drain_items():
                        if not isinstance(event, dict):
                            continue
                        key = str(event.get("subscriptionKey", ""))
                        if key.startswith("topology:"):
                            pending_topology_households.add(key.removeprefix("topology:"))
                    pending_event_at = now + EVENT_BURST_SEC
                if pending_event_at is not None and now >= pending_event_at:
                    pending_event_at = None
                    if pending_topology_households:
                        self.controller.refresh_event_topologies(
                            pending_topology_households
                        )
                        pending_topology_households.clear()
                    self.emit_snapshot(output, rediscover=False)
                    continue
                if not readable:
                    self.emit_snapshot(output)
                    continue
                if input_stream not in readable:
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
        finally:
            self.event_subscriptions.close()
