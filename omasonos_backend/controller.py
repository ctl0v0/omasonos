from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from .model import (
    choose_target_group,
    clamp_volume,
    format_sonos_time,
    group_label,
    parse_sonos_time,
)
from .state import PersistentState

LOG = logging.getLogger(__name__)


class ControllerError(RuntimeError):
    pass


class SonosController:
    """Authoritative local Sonos state and serialized command execution."""

    def __init__(
        self,
        *,
        discover_fn: Callable[..., Any] | None = None,
        soco_factory: Callable[[str], Any] | None = None,
        persistent_state: PersistentState | None = None,
    ) -> None:
        self._discover_fn = discover_fn
        self._soco_factory = soco_factory
        self.state = persistent_state or PersistentState.load()
        self._zones: dict[str, Any] = {}
        self._target_group: Any | None = None
        self._target_household_id = ""
        self._last_snapshot: dict[str, Any] = self._empty_snapshot("discovering")
        self._backend_error = ""

    @staticmethod
    def _empty_snapshot(status: str = "offline", message: str = "") -> dict[str, Any]:
        return {
            "type": "snapshot",
            "version": 1,
            "status": {
                "state": status,
                "message": message,
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

    def _ensure_soco(self) -> tuple[Callable[..., Any], Callable[[str], Any]]:
        if self._discover_fn is not None and self._soco_factory is not None:
            return self._discover_fn, self._soco_factory
        try:
            import soco
        except ImportError as exc:  # pragma: no cover - bootstrap owns this in prod
            raise ControllerError("SoCo is not installed") from exc
        self._discover_fn = self._discover_fn or soco.discover
        self._soco_factory = self._soco_factory or soco.SoCo
        return self._discover_fn, self._soco_factory

    @staticmethod
    def _safe(call: Callable[[], Any], default: Any = None) -> Any:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - network devices fail in many ways
            LOG.debug("Sonos query failed: %s", exc)
            return default

    @staticmethod
    def _zone_uid(zone: Any) -> str:
        return str(getattr(zone, "uid", "") or "")

    @staticmethod
    def _zone_name(zone: Any) -> str:
        return str(getattr(zone, "player_name", "") or "Sonos")

    def _discover_zones(self) -> list[Any]:
        discover, factory = self._ensure_soco()
        found: dict[str, Any] = {}

        # Cached addresses make cold starts useful even when SSDP is flaky.
        for host in self.state.cached_hosts:
            try:
                zone = factory(host)
                uid = self._safe(lambda z=zone: z.uid, "")
                if uid:
                    found[str(uid)] = zone
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Cached Sonos host %s unavailable: %s", host, exc)

        try:
            discovered = discover(timeout=2) or set()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Sonos discovery failed: %s", exc)
            discovered = set()

        for zone in discovered:
            uid = self._zone_uid(zone)
            if uid:
                found[uid] = zone

        zones = list(found.values())
        hosts = sorted(
            set(self.state.cached_hosts)
            | {
                str(getattr(zone, "ip_address", "") or "")
                for zone in zones
                if getattr(zone, "ip_address", "")
            }
        )
        if hosts != self.state.cached_hosts:
            self.state.cached_hosts = hosts
            self._save_state_quietly()
        return zones

    def _save_state_quietly(self) -> None:
        try:
            self.state.save()
        except OSError as exc:
            LOG.warning("Could not persist OmaSonos state: %s", exc)

    def refresh(self) -> dict[str, Any]:
        try:
            zones = self._discover_zones()
        except ControllerError as exc:
            self._last_snapshot = self._empty_snapshot("setup_error", str(exc))
            self._last_snapshot["selectedAnchorRoomUid"] = self.state.selected_room_uid
            return self._last_snapshot

        if not zones:
            self._zones = {}
            self._target_group = None
            self._last_snapshot = self._empty_snapshot(
                "offline", "No Sonos speakers found on the local network"
            )
            self._last_snapshot["selectedAnchorRoomUid"] = self.state.selected_room_uid
            return self._last_snapshot

        # A discovered bonded component can expose hidden zones. visible_zones on
        # a household member gives us logical user-facing rooms only.
        logical: dict[str, Any] = {}
        for zone in zones:
            visible = self._safe(lambda z=zone: z.visible_zones, None)
            candidates: Iterable[Any] = visible if visible is not None else [zone]
            for candidate in candidates:
                uid = self._zone_uid(candidate)
                if uid:
                    logical[uid] = candidate
        self._zones = logical

        by_household: dict[str, list[Any]] = defaultdict(list)
        for zone in logical.values():
            household_id = str(
                self._safe(lambda z=zone: z.household_id, "unknown") or "unknown"
            )
            by_household[household_id].append(zone)

        household_models: list[dict[str, Any]] = []
        all_group_models: list[dict[str, Any]] = []
        group_objects: dict[str, tuple[str, Any]] = {}

        for household_id, household_zones in sorted(by_household.items()):
            representative = household_zones[0]
            groups = self._safe(lambda z=representative: list(z.all_groups), []) or []
            room_models = [self._room_model(zone) for zone in household_zones]
            room_models.sort(key=lambda room: room["name"].lower())
            group_models: list[dict[str, Any]] = []
            for group in groups:
                model = self._group_model(group)
                if not model["memberUids"]:
                    continue
                group_models.append(model)
                all_group_models.append({**model, "householdId": household_id})
                group_objects[model["uid"]] = (household_id, group)
            group_models.sort(key=lambda group: group["label"].lower())
            household_models.append(
                {
                    "id": household_id,
                    "rooms": room_models,
                    "groups": group_models,
                }
            )

        target_model = choose_target_group(all_group_models, self.state.selected_room_uid)
        target: dict[str, Any] | None = None
        playback = self._empty_snapshot()["playback"]
        self._target_group = None
        self._target_household_id = ""

        if target_model is not None:
            target_uid = target_model["uid"]
            household_id, target_group_obj = group_objects[target_uid]
            self._target_group = target_group_obj
            self._target_household_id = household_id
            coordinator = target_group_obj.coordinator
            playback = self._playback_model(coordinator)
            target = {
                "householdId": household_id,
                "groupUid": target_uid,
                "coordinatorUid": target_model["coordinatorUid"],
                "roomLabel": target_model["label"],
                "memberUids": target_model["memberUids"],
                "volume": target_model["volume"],
                "mute": target_model["mute"],
            }

            # Persist a stable room identity, not a transient group/coordinator id.
            member_uids = target_model["memberUids"]
            if self.state.selected_room_uid not in member_uids and member_uids:
                self.state.selected_room_uid = member_uids[0]
                self._save_state_quietly()

        self._last_snapshot = {
            "type": "snapshot",
            "version": 1,
            "status": {
                "state": "ready",
                "message": "",
                "lastRefreshEpochMs": int(time.time() * 1000),
            },
            "selectedAnchorRoomUid": self.state.selected_room_uid,
            "targetGroupUid": target["groupUid"] if target else "",
            "households": household_models,
            "target": target,
            "playback": playback,
        }
        return self._last_snapshot

    def _room_model(self, zone: Any) -> dict[str, Any]:
        return {
            "uid": self._zone_uid(zone),
            "name": self._zone_name(zone),
            "ip": str(getattr(zone, "ip_address", "") or ""),
            "online": True,
            "volume": clamp_volume(self._safe(lambda z=zone: z.volume, 0)),
            "mute": bool(self._safe(lambda z=zone: z.mute, False)),
        }

    def _group_model(self, group: Any) -> dict[str, Any]:
        coordinator = group.coordinator
        members = sorted(
            list(group.members),
            key=lambda member: (self._zone_name(member).lower(), self._zone_uid(member)),
        )
        member_uids = [self._zone_uid(member) for member in members]
        names = [self._zone_name(member) for member in members]
        transport = self._safe(lambda c=coordinator: c.get_current_transport_info(), {}) or {}
        state = str(transport.get("current_transport_state", "STOPPED") or "STOPPED")
        group_uid = self._zone_uid(coordinator)
        return {
            "uid": group_uid,
            "coordinatorUid": group_uid,
            "memberUids": member_uids,
            "label": group_label(names),
            "volume": clamp_volume(self._safe(lambda g=group: g.volume, 0)),
            "mute": bool(self._safe(lambda g=group: g.mute, False)),
            "playbackState": state,
        }

    def _playback_model(self, coordinator: Any) -> dict[str, Any]:
        track = self._safe(lambda: coordinator.get_current_track_info(), {}) or {}
        transport = self._safe(lambda: coordinator.get_current_transport_info(), {}) or {}
        actions = self._safe(lambda: coordinator.available_actions, []) or []
        source = self._safe(lambda: coordinator.music_source, "UNKNOWN") or "UNKNOWN"
        artwork = str(track.get("album_art", "") or "")
        if artwork.startswith("/"):
            artwork = f"http://{coordinator.ip_address}:1400{artwork}"
        return {
            "state": str(
                transport.get("current_transport_state", "STOPPED") or "STOPPED"
            ),
            "title": str(track.get("title", "") or ""),
            "artist": str(track.get("artist", "") or ""),
            "album": str(track.get("album", "") or ""),
            "artworkUrl": artwork,
            "source": str(source),
            "positionSec": parse_sonos_time(track.get("position")),
            "durationSec": parse_sonos_time(track.get("duration")),
            "availableActions": sorted({str(action) for action in actions}),
        }

    def _coordinator(self) -> Any:
        if self._target_group is None:
            self.refresh()
        if self._target_group is None:
            raise ControllerError("No target Sonos group is available")
        return self._target_group.coordinator

    def _zone(self, uid: str) -> Any:
        if uid not in self._zones:
            self.refresh()
        zone = self._zones.get(uid)
        if zone is None:
            raise ControllerError(f"Unknown or offline room: {uid}")
        return zone

    def select_group(self, group_uid: str) -> None:
        snapshot = self.refresh()
        for household in snapshot["households"]:
            for group in household["groups"]:
                if group["uid"] == group_uid and group["memberUids"]:
                    self.state.selected_room_uid = group["memberUids"][0]
                    self._save_state_quietly()
                    return
        raise ControllerError(f"Unknown Sonos group: {group_uid}")

    def play_pause(self) -> None:
        coordinator = self._coordinator()
        transport = coordinator.get_current_transport_info()
        if str(transport.get("current_transport_state", "")).upper() == "PLAYING":
            coordinator.pause()
        else:
            coordinator.play()

    def play(self) -> None:
        self._coordinator().play()

    def pause(self) -> None:
        self._coordinator().pause()

    def next(self) -> None:
        self._coordinator().next()

    def previous(self) -> None:
        self._coordinator().previous()

    def seek(self, position_sec: Any) -> None:
        self._coordinator().seek(format_sonos_time(max(0, int(position_sec))))

    def set_group_volume(self, volume: Any) -> None:
        if self._target_group is None:
            self.refresh()
        if self._target_group is None:
            raise ControllerError("No target Sonos group is available")
        self._target_group.volume = clamp_volume(volume)

    def adjust_group_volume(self, delta: Any) -> None:
        if self._target_group is None:
            self.refresh()
        if self._target_group is None:
            raise ControllerError("No target Sonos group is available")
        self._target_group.set_relative_volume(int(delta))

    def set_group_mute(self, mute: Any) -> None:
        if self._target_group is None:
            self.refresh()
        if self._target_group is None:
            raise ControllerError("No target Sonos group is available")
        self._target_group.mute = bool(mute)

    def set_room_volume(self, room_uid: str, volume: Any) -> None:
        self._zone(room_uid).volume = clamp_volume(volume)

    def adjust_room_volume(self, room_uid: str, delta: Any) -> None:
        self._zone(room_uid).set_relative_volume(int(delta))

    def set_room_mute(self, room_uid: str, mute: Any) -> None:
        self._zone(room_uid).mute = bool(mute)

    def apply_members(self, room_uids: list[str]) -> None:
        """Reconcile selected logical rooms around the current coordinator.

        The old coordinator is removed last if it is not retained. We refresh
        after each topology mutation so the next decision is based on Sonos's
        authoritative state rather than a hoped-for transaction.
        """
        requested = list(dict.fromkeys(str(uid) for uid in room_uids if uid))
        if not requested:
            raise ControllerError("A Sonos group must contain at least one room")

        snapshot = self.refresh()
        target = snapshot.get("target")
        if not target:
            raise ControllerError("No target Sonos group is available")
        old_coordinator_uid = str(target["coordinatorUid"])
        requested_set = set(requested)

        # Sonos households are a hard boundary. Never join across them.
        allowed_uids: set[str] = set()
        for household in snapshot["households"]:
            if household["id"] == target["householdId"]:
                allowed_uids = {room["uid"] for room in household["rooms"]}
                break
        invalid = requested_set - allowed_uids
        if invalid:
            raise ControllerError(
                "Cannot group rooms outside the active Sonos household: "
                + ", ".join(sorted(invalid))
            )

        retained_uid = requested[0]
        if old_coordinator_uid in requested_set:
            retained_uid = old_coordinator_uid
        master = self._zone(old_coordinator_uid)

        # Add requested outsiders one by one to the current coordinator.
        current_members = set(target["memberUids"])
        for uid in requested:
            if uid in current_members:
                continue
            self._zone(uid).join(master)
            refreshed = self.refresh()
            current = refreshed.get("target") or {}
            current_members = set(current.get("memberUids", []))
            if uid not in current_members:
                raise ControllerError(f"Sonos did not add room {uid} to the group")

        # Remove unwanted followers first.
        refreshed = self.refresh()
        current = refreshed.get("target") or {}
        current_members = list(current.get("memberUids", []))
        for uid in current_members:
            if uid == old_coordinator_uid or uid in requested_set:
                continue
            self._zone(uid).unjoin()
            self.refresh()

        # Coordinator removal is deliberately last because Sonos elects the
        # replacement. Anchor to a retained room before the topology shifts.
        if old_coordinator_uid not in requested_set:
            self.state.selected_room_uid = retained_uid
            self._save_state_quietly()
            detached = self._zone(old_coordinator_uid)
            detached.unjoin()
            final = self.refresh()

            # Sonos may leave the old coordinator playing by itself after it
            # becomes a standalone group. Stop it only after topology confirms
            # that detachment, matching the safety rule in PLAN.md.
            detached_is_standalone = False
            for household in final.get("households", []):
                if household.get("id") != target["householdId"]:
                    continue
                for group in household.get("groups", []):
                    if (
                        group.get("coordinatorUid") == old_coordinator_uid
                        and group.get("memberUids") == [old_coordinator_uid]
                    ):
                        detached_is_standalone = True
                        break
            if detached_is_standalone:
                detached.stop()
                final = self.refresh()
        else:
            final = self.refresh()

        actual = set((final.get("target") or {}).get("memberUids", []))
        if actual != requested_set:
            raise ControllerError(
                "Sonos partially applied the grouping request; actual members: "
                + ", ".join(sorted(actual))
            )

        self.state.selected_room_uid = retained_uid
        self._save_state_quietly()
