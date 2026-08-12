from __future__ import annotations

import logging
import hashlib
import time
import unicodedata
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

SSDP_DISCOVERY_TIMEOUT_SEC = 5
NETWORK_SCAN_RETRY_SEC = 60
NETWORK_SCAN_TIMEOUT_SEC = 0.6
NETWORK_SCAN_MIN_NETMASK = 24
NETWORK_SCAN_MAX_THREADS = 128
TOPOLOGY_SETTLE_ATTEMPTS = 20
TOPOLOGY_SETTLE_INTERVAL_SEC = 0.2


class ControllerError(RuntimeError):
    pass


class SonosController:
    """Authoritative local Sonos state and serialized command execution."""

    def __init__(
        self,
        *,
        discover_fn: Callable[..., Any] | None = None,
        soco_factory: Callable[[str], Any] | None = None,
        network_scan_fn: Callable[..., Any] | None = None,
        persistent_state: PersistentState | None = None,
    ) -> None:
        self._discover_fn = discover_fn
        self._soco_factory = soco_factory
        self._network_scan_fn = network_scan_fn
        self.state = persistent_state or PersistentState.load()
        self._zones: dict[str, Any] = {}
        self._target_group: Any | None = None
        self._target_household_id = ""
        self._last_snapshot: dict[str, Any] = self._empty_snapshot("discovering")
        self._backend_error = ""
        self._last_network_scan_monotonic = -NETWORK_SCAN_RETRY_SEC
        self._discovery_diagnostics: dict[str, Any] = {}
        self._favorite_objects: dict[str, tuple[str, str]] = {}
        self._favorites_model: dict[str, Any] = {
            "state": "not_loaded",
            "items": [],
            "total": 0,
            "unsupported": 0,
            "error": "",
        }
        self._favorites_loaded = False

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
            },
        }

    def _ensure_soco(
        self,
    ) -> tuple[Callable[..., Any], Callable[[str], Any], Callable[..., Any]]:
        if (
            self._discover_fn is not None
            and self._soco_factory is not None
            and self._network_scan_fn is not None
        ):
            return self._discover_fn, self._soco_factory, self._network_scan_fn
        try:
            import soco
            from soco.discovery import scan_network
        except ImportError as exc:  # pragma: no cover - bootstrap owns this in prod
            raise ControllerError("SoCo is not installed") from exc
        self._discover_fn = self._discover_fn or soco.discover
        self._soco_factory = self._soco_factory or soco.SoCo
        self._network_scan_fn = self._network_scan_fn or scan_network
        return self._discover_fn, self._soco_factory, self._network_scan_fn

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
    def _clean_name(value: Any, fallback: str) -> str:
        name = str(value or fallback)
        # Some Sonos room names arrive with a leading variation selector or
        # zero-width formatting mark. It renders as indentation in QML even
        # though there is no visible glyph.
        while name and (
            name[0].isspace() or unicodedata.category(name[0]) in {"Cf", "Mn", "Me"}
        ):
            name = name[1:]
        return name or fallback

    @classmethod
    def _zone_name(cls, zone: Any) -> str:
        return cls._clean_name(getattr(zone, "player_name", ""), "Sonos")

    def _discover_zones(self) -> list[Any]:
        discover, factory, scan_network = self._ensure_soco()
        found: dict[str, Any] = {}
        errors: list[str] = []
        cached_hosts_tried = len(self.state.cached_hosts)
        cached_found = 0
        ssdp_found = 0
        scan_found = 0
        scan_attempted = False
        attempts: list[dict[str, Any]] = []

        # Cached addresses make cold starts useful even when SSDP is flaky.
        for host in self.state.cached_hosts:
            try:
                zone = factory(host)
                uid = self._safe(lambda z=zone: z.uid, "")
                if uid:
                    found[str(uid)] = zone
                    cached_found += 1
                    attempts.append(
                        {"method": "cache", "target": host, "result": "found"}
                    )
                else:
                    attempts.append(
                        {"method": "cache", "target": host, "result": "no-response"}
                    )
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Cached Sonos host %s unavailable: %s", host, exc)
                errors.append(f"cached {host}: {type(exc).__name__}")
                attempts.append(
                    {
                        "method": "cache",
                        "target": host,
                        "result": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        # A successful cached speaker exposes the household's current
        # ``visible_zones``, including topology changes and newly added rooms.
        # Do not stack a five-second SSDP wait onto every poll and command when
        # that direct path is healthy. SSDP remains the recovery path when all
        # cached addresses miss (including a first run with an empty cache).
        if found:
            discovered = set()
            attempts.append(
                {
                    "method": "ssdp",
                    "result": "skipped",
                    "reason": "cache-found",
                }
            )
        else:
            # SoCo's own default is five seconds. Keep that full window for
            # recovery on real Wi-Fi and multi-interface machines.
            try:
                discovered = discover(timeout=SSDP_DISCOVERY_TIMEOUT_SEC) or set()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Sonos discovery failed: %s", exc)
                errors.append(f"ssdp: {type(exc).__name__}: {exc}")
                discovered = set()
                attempts.append(
                    {
                        "method": "ssdp",
                        "result": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                attempts.append(
                    {
                        "method": "ssdp",
                        "result": "complete",
                        "found": len(discovered),
                    }
                )

        for zone in discovered:
            uid = self._zone_uid(zone)
            if uid:
                found[uid] = zone
                ssdp_found += 1

        # SSDP is UDP multicast and can be filtered by AP isolation, VLANs,
        # firewalls, or a quirky interface route. SoCo ships an explicit
        # attached-network scanner for this situation. Run it only when cache
        # and SSDP both miss, and rate-limit retries so an offline household
        # does not cause a /24 scan every polling tick.
        now = time.monotonic()
        scan_due = now - self._last_network_scan_monotonic >= NETWORK_SCAN_RETRY_SEC
        if not found and scan_due:
            scan_attempted = True
            self._last_network_scan_monotonic = now
            try:
                scanned = scan_network(
                    multi_household=True,
                    scan_timeout=NETWORK_SCAN_TIMEOUT_SEC,
                    min_netmask=NETWORK_SCAN_MIN_NETMASK,
                    max_threads=NETWORK_SCAN_MAX_THREADS,
                ) or set()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Sonos network-scan fallback failed: %s", exc)
                errors.append(f"network_scan: {type(exc).__name__}: {exc}")
                scanned = set()
                attempts.append(
                    {
                        "method": "network-scan",
                        "result": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                attempts.append(
                    {
                        "method": "network-scan",
                        "result": "complete",
                        "found": len(scanned),
                    }
                )

            for zone in scanned:
                uid = self._zone_uid(zone)
                if uid:
                    found[uid] = zone
                    scan_found += 1
        elif found:
            attempts.append(
                {"method": "network-scan", "result": "skipped", "reason": "already-found"}
            )
        else:
            attempts.append(
                {"method": "network-scan", "result": "skipped", "reason": "rate-limited"}
            )

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

        self._discovery_diagnostics = {
            "cachedHostsTried": cached_hosts_tried,
            "cachedHostsFound": cached_found,
            "ssdpTimeoutSec": SSDP_DISCOVERY_TIMEOUT_SEC,
            "ssdpFound": ssdp_found,
            "networkScanAttempted": scan_attempted,
            "networkScanFound": scan_found,
            "networkScanRetrySec": NETWORK_SCAN_RETRY_SEC,
            "attempts": attempts,
            "errors": errors,
        }
        return zones

    def _save_state_quietly(self) -> None:
        try:
            self.state.save()
        except OSError as exc:
            LOG.warning("Could not persist OmaSonos state: %s", exc)

    def event_services(self) -> dict[str, Any]:
        """Return stable event-service identities for the current topology."""
        if not self._zones:
            return {}
        services: dict[str, Any] = {}
        representative = next(iter(self._zones.values()))
        topology = getattr(representative, "zoneGroupTopology", None)
        if topology is not None:
            services["topology"] = topology
        if self._target_group is not None:
            coordinator = self._target_group.coordinator
            group_rendering = getattr(coordinator, "groupRenderingControl", None)
            if group_rendering is not None:
                services[f"group-volume:{self._zone_uid(coordinator)}"] = group_rendering

        # Playback indicators cover every room, including independent sessions
        # outside the selected group. Subscribe once per group coordinator so
        # those indicators update immediately on play/pause transitions.
        transport_coordinators: set[str] = set()
        for zone in self._zones.values():
            group = self._safe(lambda z=zone: z.group, None)
            coordinator = getattr(group, "coordinator", None)
            uid = self._zone_uid(coordinator)
            if not uid or uid in transport_coordinators:
                continue
            transport_coordinators.add(uid)
            transport = getattr(coordinator, "avTransport", None)
            if transport is not None:
                services[f"transport:{uid}"] = transport
        for uid, zone in self._zones.items():
            rendering = getattr(zone, "renderingControl", None)
            if rendering is not None:
                services[f"room-volume:{uid}"] = rendering
        return services

    def refresh(self, *, rediscover: bool = True) -> dict[str, Any]:
        if not rediscover and self._zones:
            zones = list(self._zones.values())
        else:
            try:
                zones = self._discover_zones()
            except ControllerError as exc:
                self._last_snapshot = self._empty_snapshot("setup_error", str(exc))
                self._last_snapshot["status"]["discovery"] = dict(
                    self._discovery_diagnostics
                )
                self._last_snapshot["selectedAnchorRoomUid"] = (
                    self.state.selected_room_uid
                )
                return self._last_snapshot

        if not zones:
            self._zones = {}
            self._target_group = None
            self._last_snapshot = self._empty_snapshot(
                "offline",
                "Not connected to your Sonos network",
            )
            self._last_snapshot["status"]["discovery"] = dict(
                self._discovery_diagnostics
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
            for room in room_models:
                room["playbackState"] = "STOPPED"
                for group in group_models:
                    if room["uid"] in group["memberUids"]:
                        room["playbackState"] = group["playbackState"]
                        break
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

            if not self._favorites_loaded:
                self.refresh_favorites()

        self._last_snapshot = {
            "type": "snapshot",
            "version": 1,
            "status": {
                "state": "ready",
                "message": "",
                "lastRefreshEpochMs": int(time.time() * 1000),
                "discovery": dict(self._discovery_diagnostics),
            },
            "selectedAnchorRoomUid": self.state.selected_room_uid,
            "targetGroupUid": target["groupUid"] if target else "",
            "households": household_models,
            "target": target,
            "favorites": dict(self._favorites_model),
            "playback": playback,
        }
        return self._last_snapshot

    @staticmethod
    def _favorite_kind(uri: str) -> str:
        radio_prefixes = (
            "x-sonosapi-stream:",
            "x-sonosapi-radio:",
            "x-rincon-mp3radio:",
            "hls-radio:",
        )
        return "radio" if uri.lower().startswith(radio_prefixes) else "audio"

    @staticmethod
    def _favorite_is_directly_playable(uri: str) -> bool:
        """Keep the MVP on URI types Sonos accepts via SetAVTransportURI.

        In particular, ``x-rincon-cpcontainer`` Favorites are albums, mixes,
        or playlists which must be expanded into a queue before playback.
        Treating those as direct audio produces UPnP 714 on real speakers.
        """
        direct_prefixes = (
            "http:",
            "https:",
            "x-file-cifs:",
            "x-rincon-mp3radio:",
            "x-sonos-http:",
            "x-sonos-https:",
            "x-sonosapi-radio:",
            "x-sonosapi-stream:",
            "hls-radio:",
        )
        return uri.lower().startswith(direct_prefixes)

    def refresh_favorites(self) -> None:
        self._favorites_loaded = True
        self._favorite_objects = {}
        if self._target_group is None:
            self._favorites_model = {
                "state": "error",
                "items": [],
                "total": 0,
                "unsupported": 0,
                "error": "No target Sonos group is available",
            }
            return
        try:
            coordinator = self._target_group.coordinator
            result = coordinator.music_library.get_sonos_favorites(
                complete_result=True,
                max_items=100,
            )
            favorites = list(result)
            total = int(getattr(result, "total_matches", len(favorites)))
            items: list[dict[str, str]] = []
            for favorite in favorites:
                resources = list(getattr(favorite, "resources", []) or [])
                uri = str(getattr(resources[0], "uri", "") or "") if resources else ""
                metadata = str(getattr(favorite, "resource_meta_data", "") or "")
                title = self._clean_name(getattr(favorite, "title", ""), "Favorite")
                if not uri or not metadata or not self._favorite_is_directly_playable(uri):
                    continue
                favorite_id = hashlib.sha256(
                    f"{title}\0{uri}\0{metadata}".encode("utf-8")
                ).hexdigest()[:20]
                self._favorite_objects[favorite_id] = (uri, metadata)
                items.append(
                    {
                        "id": favorite_id,
                        "title": title,
                        "kind": self._favorite_kind(uri),
                    }
                )
            self._favorites_model = {
                "state": "ready",
                "items": items,
                "total": total,
                "unsupported": max(0, total - len(items)),
                "error": "",
            }
        except Exception as exc:  # noqa: BLE001 - favorites must not break control
            LOG.warning("Could not load Sonos Favorites: %s", exc)
            self._favorites_model = {
                "state": "error",
                "items": [],
                "total": 0,
                "unsupported": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def play_favorite(self, favorite_id: str) -> None:
        favorite = self._favorite_objects.get(favorite_id)
        if favorite is None:
            raise ControllerError("Unknown or unavailable Sonos Favorite")
        uri, metadata = favorite
        self._coordinator().play_uri(uri=uri, meta=metadata, start=True)

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

    def _clear_topology_caches(self) -> None:
        """Force subsequent SoCo group reads to query authoritative topology."""
        for zone in self._zones.values():
            topology = getattr(zone, "zone_group_state", None)
            clear_cache = getattr(topology, "clear_cache", None)
            if callable(clear_cache):
                self._safe(clear_cache)

    @staticmethod
    def _snapshot_group_for_room(
        snapshot: dict[str, Any], household_id: str, room_uid: str
    ) -> dict[str, Any] | None:
        for household in snapshot.get("households", []):
            if household.get("id") != household_id:
                continue
            for group in household.get("groups", []):
                if room_uid in group.get("memberUids", []):
                    return group
        return None

    def _wait_for_topology(
        self, predicate: Callable[[dict[str, Any]], bool]
    ) -> dict[str, Any]:
        """Poll briefly while Sonos and SoCo converge after a group mutation."""
        latest: dict[str, Any] = {}
        for attempt in range(TOPOLOGY_SETTLE_ATTEMPTS):
            # SoCo's join/unjoin methods clear only the mutated speaker's
            # cache. Refresh may use another household member as its topology
            # representative, so invalidate all known views before checking.
            self._clear_topology_caches()
            latest = self.refresh(rediscover=False)
            if predicate(latest):
                return latest
            if attempt + 1 < TOPOLOGY_SETTLE_ATTEMPTS:
                time.sleep(TOPOLOGY_SETTLE_INTERVAL_SEC)
        return latest

    def select_group(self, group_uid: str) -> None:
        snapshot = self.refresh(rediscover=False)
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

    def move_playback_to_room(self, room_uid: str) -> None:
        """Select a room, moving the current session only while it is playing.

        When the selected session is paused or stopped, this is only a control
        target change and never mutates topology. For playing audio, rooms in a
        different multi-room group remain protected because joining one would
        dismantle that group as a side effect; those changes belong to the
        explicit group-settings operation.
        """
        snapshot = self.refresh(rediscover=False)
        target = snapshot.get("target")
        if not target:
            raise ControllerError("No target Sonos group is available")

        household = next(
            (
                item
                for item in snapshot.get("households", [])
                if item.get("id") == target.get("householdId")
            ),
            None,
        )
        if household is None or room_uid not in {
            room.get("uid") for room in household.get("rooms", [])
        }:
            raise ControllerError("The selected room is unavailable")

        source_was_playing = (
            str(snapshot.get("playback", {}).get("state", "")).upper() == "PLAYING"
        )
        if not source_was_playing:
            self.state.selected_room_uid = room_uid
            self._save_state_quietly()
            return

        current_members = set(target.get("memberUids", []))
        if len(current_members) > 1:
            raise ControllerError(
                "Current audio is playing on a group. Change that group "
                "deliberately in Group settings before moving to one room."
            )
        for group in household.get("groups", []):
            members = set(group.get("memberUids", []))
            if room_uid in members and len(members) > 1:
                raise ControllerError(
                    "That room belongs to another group. Change groups deliberately "
                    "in Group settings first."
                )

        source_uid = str(target.get("coordinatorUid", ""))
        if room_uid == source_uid:
            return

        source = self._zone(source_uid)
        destination = self._zone(room_uid)

        # Silence the source before the session handoff so joining the
        # destination never makes both rooms audible, even briefly.
        if source_was_playing:
            source.pause()

        try:
            destination.join(source)
        except Exception:
            # A failed join has not moved the session. Restore what the user
            # was listening to rather than leaving the source paused.
            if source_was_playing:
                source.play()
            raise

        household_id = str(target.get("householdId", ""))
        joined = self._wait_for_topology(
            lambda current: set(
                (
                    self._snapshot_group_for_room(current, household_id, source_uid)
                    or {}
                ).get("memberUids", [])
            )
            == {source_uid, room_uid}
        )
        joined_group = self._snapshot_group_for_room(
            joined, household_id, source_uid
        ) or {}
        joined_members = set(joined_group.get("memberUids", []))
        if joined_members != {source_uid, room_uid}:
            raise ControllerError(
                "Sonos did not prepare the selected room for the audio handoff"
            )

        # Anchor selection to the destination before detaching the old
        # coordinator. Sonos will elect the remaining room as coordinator.
        self.state.selected_room_uid = room_uid
        self._save_state_quietly()
        source.unjoin()
        final = self._wait_for_topology(
            lambda current: set(
                (
                    self._snapshot_group_for_room(current, household_id, source_uid)
                    or {}
                ).get("memberUids", [])
            )
            == {source_uid}
            and set(
                (
                    self._snapshot_group_for_room(current, household_id, room_uid)
                    or {}
                ).get("memberUids", [])
            )
            == {room_uid}
        )

        source_group = self._snapshot_group_for_room(final, household_id, source_uid) or {}
        destination_group = (
            self._snapshot_group_for_room(final, household_id, room_uid) or {}
        )
        source_members = set(source_group.get("memberUids", []))
        destination_members = set(destination_group.get("memberUids", []))
        source_state = str(source_group.get("playbackState", "")).upper()

        if source_members != {source_uid} or destination_members != {room_uid}:
            raise ControllerError(
                "Sonos partially moved the audio; the rooms are not standalone"
            )

        # Coordinator changes can restart the detached source. If that
        # happened, pause it again after topology confirms it is independent,
        # then resume only the new destination.
        if source_was_playing:
            if source_state == "PLAYING":
                source.pause()
            destination.play()
            self.refresh(rediscover=False)

        self.state.selected_room_uid = room_uid
        self._save_state_quietly()

    def apply_members(self, room_uids: list[str]) -> None:
        """Reconcile selected logical rooms around the current coordinator.

        The old coordinator is removed last if it is not retained. We refresh
        after each topology mutation so the next decision is based on Sonos's
        authoritative state rather than a hoped-for transaction.
        """
        requested = list(dict.fromkeys(str(uid) for uid in room_uids if uid))
        if not requested:
            raise ControllerError("A Sonos group must contain at least one room")

        snapshot = self.refresh(rediscover=False)
        target = snapshot.get("target")
        if not target:
            raise ControllerError("No target Sonos group is available")
        old_coordinator_uid = str(target["coordinatorUid"])
        requested_set = set(requested)
        source_was_playing = (
            str(snapshot.get("playback", {}).get("state", "")).upper() == "PLAYING"
        )

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
            refreshed = self.refresh(rediscover=False)
            current = refreshed.get("target") or {}
            current_members = set(current.get("memberUids", []))
            if uid not in current_members:
                raise ControllerError(f"Sonos did not add room {uid} to the group")

        # Remove unwanted followers first.
        refreshed = self.refresh(rediscover=False)
        current = refreshed.get("target") or {}
        current_members = list(current.get("memberUids", []))
        for uid in current_members:
            if uid == old_coordinator_uid or uid in requested_set:
                continue
            self._zone(uid).unjoin()
            self.refresh(rediscover=False)

        # Coordinator removal is deliberately last because Sonos elects the
        # replacement. Anchor to a retained room before the topology shifts.
        if old_coordinator_uid not in requested_set:
            self.state.selected_room_uid = retained_uid
            self._save_state_quietly()
            detached = self._zone(old_coordinator_uid)
            detached.unjoin()
            final = self.refresh(rediscover=False)

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
                final = self.refresh(rediscover=False)
        else:
            final = self.refresh(rediscover=False)

        actual = set((final.get("target") or {}).get("memberUids", []))
        if actual != requested_set:
            raise ControllerError(
                "Sonos partially applied the grouping request; actual members: "
                + ", ".join(sorted(actual))
            )

        # Coordinator handoff can leave the retained room paused even though
        # the source was playing. Restore only an observed PLAYING state; do
        # not start audio that the user had paused before moving it.
        if (
            source_was_playing
            and str(final.get("playback", {}).get("state", "")).upper()
            != "PLAYING"
        ):
            self._coordinator().play()
            final = self.refresh(rediscover=False)

        self.state.selected_room_uid = retained_uid
        self._save_state_quietly()
