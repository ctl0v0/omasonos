from __future__ import annotations

import logging
import hashlib
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import unquote, urlsplit

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
# Coordinator removal is substantially slower than joining on current Sonos
# S2 firmware (observed at roughly 10-12 seconds on the target household).
TOPOLOGY_SETTLE_ATTEMPTS = 75
TOPOLOGY_SETTLE_INTERVAL_SEC = 0.2
TOPOLOGY_QUERY_TIMEOUT_SEC = 2.0
PLAYBACK_QUERY_ATTEMPTS = 2
PLAYBACK_QUERY_RETRY_SEC = 0.08


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
        self._favorite_objects: dict[str, dict[str, Any]] = {}
        self._favorites_model: dict[str, Any] = {
            "state": "not_loaded",
            "items": [],
            "total": 0,
            "unsupported": 0,
            "error": "",
        }
        self._favorites_loaded = False
        self._favorites_household_id = ""
        self._transport_state_cache: dict[str, str] = {}
        self._playback_cache: dict[str, dict[str, Any]] = {}

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
                "metadataState": "empty",
                "stale": False,
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
    def _query_with_retry(
        label: str,
        call: Callable[[], Any],
        default: Any,
    ) -> tuple[bool, Any]:
        """Distinguish a real empty Sonos response from a network failure."""
        for attempt in range(PLAYBACK_QUERY_ATTEMPTS):
            try:
                return True, call()
            except Exception as exc:  # noqa: BLE001 - LAN devices fail transiently
                LOG.debug(
                    "Sonos %s query failed (%s/%s): %s",
                    label,
                    attempt + 1,
                    PLAYBACK_QUERY_ATTEMPTS,
                    exc,
                )
                if attempt + 1 < PLAYBACK_QUERY_ATTEMPTS:
                    time.sleep(PLAYBACK_QUERY_RETRY_SEC)
        return False, default

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
        household_representatives: dict[str, Any] = {}
        for zone in self._zones.values():
            household_id = str(
                self._safe(lambda z=zone: z.household_id, "unknown") or "unknown"
            )
            household_representatives.setdefault(household_id, zone)
        for household_id, representative in household_representatives.items():
            topology = getattr(representative, "zoneGroupTopology", None)
            if topology is not None:
                services[f"topology:{household_id}"] = topology
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
            playback = self._playback_model(
                coordinator,
                state_hint=target_model["playbackState"],
            )
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

            if self._favorites_household_id != household_id:
                self._favorites_loaded = False
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
                "playbackDegraded": bool(playback.get("stale", False)),
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
    def _favorite_reference(favorite: Any) -> Any | None:
        try:
            return favorite.reference
        except Exception as exc:  # noqa: BLE001 - malformed favorites are common
            LOG.debug("Could not parse Sonos Favorite reference: %s", exc)
            return None

    @staticmethod
    def _tunein_podcast_id(reference: Any) -> str:
        """Return the TuneIn container id embedded in a Sonos Favorite.

        TuneIn (New) favorites use an AppLink account that SoCo cannot read
        back from the speaker. Podcast ids remain browseable through TuneIn's
        anonymous legacy Sonos service, however, so no developer or user token
        is required.
        """
        desc = str(getattr(reference, "desc", "") or "")
        item_id = str(getattr(reference, "item_id", "") or "")
        if "85255" not in desc or not item_id.startswith("100b2064"):
            return ""
        return unquote(item_id.removeprefix("100b2064"))

    @staticmethod
    def _tunein_service(coordinator: Any) -> Any:
        from soco.music_services import MusicService

        return MusicService("TuneIn", device=coordinator)

    @staticmethod
    def _tunein_media_url(service: Any, episode: Any) -> str:
        """Resolve TuneIn's short M3U response to a direct episode URL."""
        import requests

        media_uri = str(service.get_media_uri(episode.id) or "")
        if urlsplit(media_uri).scheme not in {"http", "https"}:
            raise ControllerError("TuneIn returned an invalid podcast media URL")

        with requests.get(media_uri, timeout=10, stream=True) as response:
            response.raise_for_status()
            content_type = str(response.headers.get("content-type", "")).lower()
            if "mpegurl" not in content_type and not media_uri.lower().endswith(
                (".m3u", ".m3u8")
            ):
                return media_uri

            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=4096):
                size += len(chunk)
                if size > 256 * 1024:
                    raise ControllerError("TuneIn returned an oversized podcast playlist")
                chunks.append(chunk)

        playlist = b"".join(chunks).decode("utf-8", errors="replace")
        for line in playlist.splitlines():
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue
            if urlsplit(candidate).scheme in {"http", "https"}:
                return candidate
        raise ControllerError("TuneIn returned an empty podcast playlist")

    @staticmethod
    def _podcast_playback_metadata(episode: Any, media_url: str) -> str:
        """Build rich DIDL metadata for a resolved TuneIn podcast episode."""
        episode_metadata = getattr(episode, "metadata", {})
        if not isinstance(episode_metadata, dict):
            episode_metadata = {}
        track_metadata = getattr(episode_metadata.get("track_metadata"), "metadata", {})
        if not isinstance(track_metadata, dict):
            track_metadata = {}

        title = str(getattr(episode, "title", "") or "Podcast")
        show = str(
            track_metadata.get("podcast")
            or track_metadata.get("associated_show")
            or ""
        )
        artist = str(
            track_metadata.get("host")
            or track_metadata.get("artist")
            or track_metadata.get("producer")
            or ""
        )
        artwork = str(track_metadata.get("album_art_uri") or "")
        if urlsplit(artwork).scheme not in {"http", "https"}:
            artwork = ""

        didl_ns = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
        dc_ns = "http://purl.org/dc/elements/1.1/"
        upnp_ns = "urn:schemas-upnp-org:metadata-1-0/upnp/"
        rincon_ns = "urn:schemas-rinconnetworks-com:metadata-1-0/"
        ET.register_namespace("", didl_ns)
        ET.register_namespace("dc", dc_ns)
        ET.register_namespace("upnp", upnp_ns)
        ET.register_namespace("r", rincon_ns)
        root = ET.Element(f"{{{didl_ns}}}DIDL-Lite")
        item = ET.SubElement(
            root,
            f"{{{didl_ns}}}item",
            {"id": "R:0/0/0", "parentID": "R:0/0", "restricted": "true"},
        )
        ET.SubElement(item, f"{{{dc_ns}}}title").text = title
        ET.SubElement(item, f"{{{upnp_ns}}}class").text = (
            "object.item.audioItem.musicTrack"
        )
        if artist:
            ET.SubElement(item, f"{{{dc_ns}}}creator").text = artist
            ET.SubElement(item, f"{{{upnp_ns}}}artist").text = artist
        if show:
            ET.SubElement(item, f"{{{upnp_ns}}}album").text = show
        if artwork:
            ET.SubElement(item, f"{{{upnp_ns}}}albumArtURI").text = artwork
        description = str(getattr(episode, "desc", "") or "SA_RINCON65031_")
        desc = ET.SubElement(
            item,
            f"{{{didl_ns}}}desc",
            {"id": "cdudn", "nameSpace": rincon_ns},
        )
        desc.text = description
        resource = ET.SubElement(
            item,
            f"{{{didl_ns}}}res",
            {"protocolInfo": "http-get:*:audio/mpeg:*"},
        )
        duration = track_metadata.get("duration")
        try:
            if duration is not None:
                resource.set("duration", format_sonos_time(int(duration)))
        except (TypeError, ValueError):
            pass
        resource.text = media_url
        return ET.tostring(root, encoding="unicode")

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
        self._favorites_household_id = self._target_household_id
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
                reference = self._favorite_reference(favorite) if metadata else None
                playback: dict[str, Any] | None = None
                kind = self._favorite_kind(uri)
                identity = uri
                if uri and metadata and self._favorite_is_directly_playable(uri):
                    playback = {
                        "mode": "direct",
                        "uri": uri,
                        "metadata": metadata,
                    }
                elif uri.lower().startswith("x-rincon-cpcontainer:") and reference:
                    playback = {"mode": "queue", "item": reference}
                elif reference:
                    podcast_id = self._tunein_podcast_id(reference)
                    if podcast_id:
                        playback = {
                            "mode": "tuneinPodcast",
                            "podcastId": podcast_id,
                        }
                        identity = podcast_id
                        kind = "podcast"
                if playback is None:
                    continue
                favorite_id = hashlib.sha256(
                    f"{title}\0{identity}\0{metadata}".encode("utf-8")
                ).hexdigest()[:20]
                self._favorite_objects[favorite_id] = playback
                items.append(
                    {
                        "id": favorite_id,
                        "title": title,
                        "kind": kind,
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
        coordinator = self._coordinator()
        mode = favorite["mode"]
        if mode == "direct":
            coordinator.play_uri(
                uri=favorite["uri"],
                meta=favorite["metadata"],
                start=True,
            )
            return
        if mode == "queue":
            queue_position = coordinator.add_to_queue(favorite["item"])
            coordinator.play_from_queue(queue_position - 1)
            return
        if mode == "tuneinPodcast":
            try:
                service = self._tunein_service(coordinator)
                episodes = service.get_metadata(
                    favorite["podcastId"],
                    count=1,
                )
                episode = next(iter(episodes))
                media_url = self._tunein_media_url(service, episode)
                metadata = self._podcast_playback_metadata(episode, media_url)
                coordinator.play_uri(
                    uri=media_url,
                    meta=metadata,
                    start=True,
                )
                return
            except (StopIteration, TypeError) as exc:
                raise ControllerError(
                    "TuneIn did not return a playable podcast episode"
                ) from exc
            except ControllerError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalize service failures
                raise ControllerError(
                    f"Could not load the TuneIn podcast: {type(exc).__name__}: {exc}"
                ) from exc
        raise ControllerError("Unsupported Sonos Favorite playback mode")

    def _room_model(self, zone: Any) -> dict[str, Any]:
        return {
            "uid": self._zone_uid(zone),
            "name": self._zone_name(zone),
            "ip": str(getattr(zone, "ip_address", "") or ""),
            "online": True,
            "volume": clamp_volume(self._safe(lambda z=zone: z.volume, 0)),
            "mute": bool(self._safe(lambda z=zone: z.mute, False)),
        }

    def _transport_state(self, coordinator: Any) -> tuple[str, bool]:
        uid = self._zone_uid(coordinator)
        ok, transport = self._query_with_retry(
            f"transport for {uid or 'unknown coordinator'}",
            lambda: coordinator.get_current_transport_info(),
            {},
        )
        if ok and isinstance(transport, dict):
            state = str(transport.get("current_transport_state", "") or "").upper()
            if state:
                self._transport_state_cache[uid] = state
                return state, True
        cached = self._transport_state_cache.get(uid, "UNKNOWN")
        return cached, False

    def _group_model(self, group: Any) -> dict[str, Any]:
        coordinator = group.coordinator
        members = sorted(
            list(group.members),
            key=lambda member: (self._zone_name(member).lower(), self._zone_uid(member)),
        )
        member_uids = [self._zone_uid(member) for member in members]
        names = [self._zone_name(member) for member in members]
        state, _ = self._transport_state(coordinator)
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

    @staticmethod
    def _media_metadata(response: Any) -> dict[str, str]:
        """Extract station/container metadata omitted by track-position info."""
        if not isinstance(response, dict):
            return {"title": "", "artworkUrl": "", "uri": ""}
        result = {
            "title": "",
            "artworkUrl": "",
            "uri": str(response.get("CurrentURI", "") or ""),
        }
        raw = str(response.get("CurrentURIMetaData", "") or "")
        if not raw or raw == "NOT_IMPLEMENTED":
            return result
        try:
            metadata = ET.fromstring(raw)
        except ET.ParseError as exc:
            LOG.debug("Could not parse Sonos media metadata: %s", exc)
            return result
        result["title"] = str(
            metadata.findtext(".//{http://purl.org/dc/elements/1.1/}title") or ""
        )
        result["artworkUrl"] = str(
            metadata.findtext(
                ".//{urn:schemas-upnp-org:metadata-1-0/upnp/}albumArtURI"
            )
            or ""
        )
        return result

    def _playback_model(
        self,
        coordinator: Any,
        *,
        state_hint: str = "UNKNOWN",
    ) -> dict[str, Any]:
        uid = self._zone_uid(coordinator)
        cached = self._playback_cache.get(uid, {})
        track_ok, track = self._query_with_retry(
            f"track metadata for {uid or 'unknown coordinator'}",
            lambda: coordinator.get_current_track_info(),
            {},
        )
        if not isinstance(track, dict):
            track_ok = False
            track = {}

        state, transport_ok = self._transport_state(coordinator)
        if state == "UNKNOWN" and state_hint:
            state = str(state_hint).upper()

        media_ok, media_response = self._query_with_retry(
            f"media metadata for {uid or 'unknown coordinator'}",
            lambda: coordinator.avTransport.GetMediaInfo([("InstanceID", 0)]),
            {},
        )
        media = self._media_metadata(media_response) if media_ok else {
            "title": "",
            "artworkUrl": "",
            "uri": "",
        }

        actions = self._safe(lambda: coordinator.available_actions, []) or []
        source = str(
            self._safe(lambda: coordinator.music_source, cached.get("source", "UNKNOWN"))
            or cached.get("source", "UNKNOWN")
            or "UNKNOWN"
        )
        artwork = str(track.get("album_art", "") or media["artworkUrl"] or "")
        if artwork.startswith("/"):
            artwork = f"http://{coordinator.ip_address}:1400{artwork}"

        track_title = str(track.get("title", "") or "")
        media_title = str(media["title"] or "")
        # Direct podcast streams can expose a shortened title through
        # GetPositionInfo while retaining the complete title in media metadata.
        title = track_title or media_title
        if media_title and track_title and media_title.startswith(track_title):
            title = media_title

        model: dict[str, Any] = {
            "state": state,
            "title": title,
            "artist": str(track.get("artist", "") or ""),
            "album": str(track.get("album", "") or ""),
            "artworkUrl": artwork,
            "source": source,
            "positionSec": parse_sonos_time(track.get("position")),
            "durationSec": parse_sonos_time(track.get("duration")),
            "availableActions": sorted({str(action) for action in actions}),
            "metadataState": "fresh",
            "stale": not transport_ok or not track_ok,
        }

        active = state in {"PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING"}
        used_cached_metadata = False
        if active and cached and (not track_ok or not media_ok):
            for key in ("title", "artist", "album", "artworkUrl"):
                if not model[key] and cached.get(key):
                    model[key] = cached[key]
                    used_cached_metadata = True
            if model["positionSec"] is None and not track_ok:
                model["positionSec"] = cached.get("positionSec")
            if model["durationSec"] is None and not track_ok:
                model["durationSec"] = cached.get("durationSec")
            if not model["availableActions"]:
                model["availableActions"] = list(cached.get("availableActions", []))

        if active:
            if used_cached_metadata:
                model["metadataState"] = "cached"
            elif not model["title"] and not model["artist"] and not model["artworkUrl"]:
                model["metadataState"] = "unavailable"
            self._playback_cache[uid] = dict(model)
        elif state == "STOPPED" and transport_ok:
            # A confirmed stop must not display a previous session's track.
            self._playback_cache.pop(uid, None)
            model.update(
                {
                    "title": "",
                    "artist": "",
                    "album": "",
                    "artworkUrl": "",
                    "positionSec": None,
                    "durationSec": None,
                    "metadataState": "empty",
                    "stale": False,
                }
            )
        elif cached:
            # If transport itself is unreachable, retain the last confirmed
            # session instead of manufacturing a STOPPED/blank snapshot.
            preserved = dict(cached)
            preserved["stale"] = True
            preserved["metadataState"] = "cached"
            return preserved

        return model

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

    def _household_representative(self, household_id: str = "") -> Any | None:
        for zone in self._zones.values():
            if not household_id:
                return zone
            zone_household = str(
                self._safe(lambda z=zone: z.household_id, "unknown") or "unknown"
            )
            if zone_household == household_id:
                return zone
        return None

    def _refresh_topology_authoritatively(self, household_id: str = "") -> bool:
        """Bypass SoCo's subscription-backed topology cache.

        SoCo 0.31.2 declines to poll ZoneGroupState while any subscription is
        active. The threaded events implementation does not apply the topology
        payload to ZoneGroupState, so group mutations otherwise remain stale.
        """
        representative = self._household_representative(household_id)
        if representative is None:
            return False
        service = getattr(representative, "zoneGroupTopology", None)
        topology = getattr(representative, "zone_group_state", None)
        get_state = getattr(service, "GetZoneGroupState", None)
        process_payload = getattr(topology, "process_payload", None)
        if not callable(get_state) or not callable(process_payload):
            return False
        try:
            response = get_state(timeout=TOPOLOGY_QUERY_TIMEOUT_SEC)
            payload = response.get("ZoneGroupState", "") if response else ""
            if not payload:
                return False
            process_payload(
                payload=payload,
                source="omasonos-authoritative-poll",
                source_ip=str(getattr(representative, "ip_address", "") or ""),
            )
            return True
        except Exception as exc:  # noqa: BLE001 - retry/fallback owns failures
            LOG.debug("Authoritative Sonos topology query failed: %s", exc)
            return False

    def refresh_event_topologies(self, household_ids: Iterable[str]) -> None:
        """Apply authoritative topology after SoCo's threaded event callback."""
        for household_id in sorted(set(household_ids)):
            self._refresh_topology_authoritatively(household_id)

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
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        household_id: str = "",
        phase: str = "topology mutation",
    ) -> dict[str, Any]:
        """Poll briefly while Sonos and SoCo converge after a group mutation."""
        latest: dict[str, Any] = {}
        started = time.monotonic()
        for attempt in range(TOPOLOGY_SETTLE_ATTEMPTS):
            # SoCo's join/unjoin methods clear only the mutated speaker's
            # cache. Refresh may use another household member as its topology
            # representative, so invalidate all known views before checking.
            self._clear_topology_caches()
            authoritative = self._refresh_topology_authoritatively(household_id)
            latest = self.refresh(rediscover=False)
            if predicate(latest):
                LOG.info(
                    "Sonos %s confirmed after %.2fs (%s checks, authoritative=%s)",
                    phase,
                    time.monotonic() - started,
                    attempt + 1,
                    authoritative,
                )
                return latest
            if attempt + 1 < TOPOLOGY_SETTLE_ATTEMPTS:
                time.sleep(TOPOLOGY_SETTLE_INTERVAL_SEC)
        LOG.warning(
            "Sonos %s did not converge after %.2fs (%s checks)",
            phase,
            time.monotonic() - started,
            TOPOLOGY_SETTLE_ATTEMPTS,
        )
        return latest

    def _wait_for_room_memberships(
        self,
        household_id: str,
        expected: dict[str, set[str]],
        *,
        phase: str,
    ) -> dict[str, Any]:
        """Wait on the small topology model, avoiding full household refreshes."""
        representative = self._household_representative(household_id)
        topology = getattr(representative, "zone_group_state", None)
        if topology is None or not callable(
            getattr(topology, "process_payload", None)
        ):
            return self._wait_for_topology(
                lambda current: all(
                    set(
                        (
                            self._snapshot_group_for_room(
                                current, household_id, room_uid
                            )
                            or {}
                        ).get("memberUids", [])
                    )
                    == members
                    for room_uid, members in expected.items()
                ),
                household_id=household_id,
                phase=phase,
            )

        started = time.monotonic()
        logical_uids = set(self._zones)
        observed: dict[str, set[str]] = {}
        for attempt in range(TOPOLOGY_SETTLE_ATTEMPTS):
            self._refresh_topology_authoritatively(household_id)
            observed = {}
            for group in getattr(topology, "groups", set()) or set():
                members = {
                    self._zone_uid(member)
                    for member in getattr(group, "members", set())
                    if self._zone_uid(member) in logical_uids
                }
                for uid in members:
                    observed[uid] = members
            if all(observed.get(uid, set()) == members for uid, members in expected.items()):
                LOG.info(
                    "Sonos %s confirmed after %.2fs (%s topology checks)",
                    phase,
                    time.monotonic() - started,
                    attempt + 1,
                )
                return self.refresh(rediscover=False)
            if attempt + 1 < TOPOLOGY_SETTLE_ATTEMPTS:
                time.sleep(TOPOLOGY_SETTLE_INTERVAL_SEC)
        LOG.warning(
            "Sonos %s did not converge after %.2fs; observed memberships: %s",
            phase,
            time.monotonic() - started,
            {uid: sorted(members) for uid, members in observed.items()},
        )
        return self.refresh(rediscover=False)

    def _refresh_after_topology_mutation(self, household_id: str) -> dict[str, Any]:
        self._clear_topology_caches()
        self._refresh_topology_authoritatively(household_id)
        return self.refresh(rediscover=False)

    @staticmethod
    def _play_confirmed_coordinator(zone: Any) -> None:
        """Play after snapshot verification without consulting SoCo's stale role cache."""
        try:
            zone.play()
            return
        except Exception as exc:  # noqa: BLE001 - normalize one SoCo cache defect
            if type(exc).__name__ != "SoCoSlaveException":
                raise
            LOG.info(
                "Bypassing stale SoCo coordinator role for confirmed room %s",
                SonosController._zone_uid(zone),
            )
        zone.avTransport.Play([("InstanceID", 0), ("Speed", 1)])

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

    def _rollback_audio_handoff(
        self,
        source: Any,
        destination: Any,
        source_uid: str,
        destination_uid: str,
        household_id: str,
        source_was_playing: bool,
    ) -> None:
        """Best-effort restoration of the pre-handoff standalone rooms."""
        LOG.warning(
            "Rolling back Sonos audio handoff %s -> %s",
            source_uid,
            destination_uid,
        )
        try:
            destination.unjoin()
            self._wait_for_room_memberships(
                household_id,
                {
                    source_uid: {source_uid},
                    destination_uid: {destination_uid},
                },
                phase="handoff rollback",
            )
        except Exception as exc:  # noqa: BLE001 - preserve the original error
            LOG.warning("Could not fully restore Sonos topology: %s", exc)
        self.state.selected_room_uid = source_uid
        self._save_state_quietly()
        if source_was_playing:
            try:
                self._play_confirmed_coordinator(source)
            except Exception as exc:  # noqa: BLE001 - preserve the original error
                LOG.warning("Could not resume source after handoff rollback: %s", exc)

    def _move_direct_stream(
        self,
        source: Any,
        destination: Any,
        snapshot: dict[str, Any],
    ) -> bool:
        """Move addressable streams without slow, failure-prone regrouping."""
        ok, media = self._query_with_retry(
            "media for direct room handoff",
            lambda: source.avTransport.GetMediaInfo([("InstanceID", 0)]),
            {},
        )
        if not ok or not isinstance(media, dict):
            return False
        uri = str(media.get("CurrentURI", "") or "")
        if not self._favorite_is_directly_playable(uri):
            return False
        metadata = str(media.get("CurrentURIMetaData", "") or "")
        position = snapshot.get("playback", {}).get("positionSec")
        actions = {
            str(action) for action in snapshot.get("playback", {}).get("availableActions", [])
        }

        source.pause()
        try:
            destination.avTransport.SetAVTransportURI(
                [
                    ("InstanceID", 0),
                    ("CurrentURI", uri),
                    ("CurrentURIMetaData", metadata),
                ]
            )
            if position is not None and "SeekTime" in actions:
                destination.avTransport.Seek(
                    [
                        ("InstanceID", 0),
                        ("Unit", "REL_TIME"),
                        ("Target", format_sonos_time(max(0, int(position)))),
                    ]
                )
            self._play_confirmed_coordinator(destination)
        except Exception:
            self._play_confirmed_coordinator(source)
            raise
        LOG.info(
            "Moved direct Sonos stream %s -> %s without regrouping",
            self._zone_uid(source),
            self._zone_uid(destination),
        )
        return True

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

        source_was_playing = str(
            snapshot.get("playback", {}).get("state", "")
        ).upper() in {"PLAYING", "TRANSITIONING"}
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

        if self._move_direct_stream(source, destination, snapshot):
            self.state.selected_room_uid = room_uid
            self._save_state_quietly()
            return

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
        joined = self._wait_for_room_memberships(
            household_id,
            {
                source_uid: {source_uid, room_uid},
                room_uid: {source_uid, room_uid},
            },
            phase="handoff join",
        )
        joined_group = self._snapshot_group_for_room(
            joined, household_id, source_uid
        ) or {}
        joined_members = set(joined_group.get("memberUids", []))
        if joined_members != {source_uid, room_uid}:
            self._rollback_audio_handoff(
                source,
                destination,
                source_uid,
                room_uid,
                household_id,
                source_was_playing,
            )
            raise ControllerError(
                "Sonos did not prepare the selected room for the audio handoff"
            )

        # Anchor selection to the destination before detaching the old
        # coordinator. Sonos will elect the remaining room as coordinator.
        self.state.selected_room_uid = room_uid
        self._save_state_quietly()
        source.unjoin()
        final = self._wait_for_room_memberships(
            household_id,
            {source_uid: {source_uid}, room_uid: {room_uid}},
            phase="handoff detach",
        )

        source_group = self._snapshot_group_for_room(final, household_id, source_uid) or {}
        destination_group = (
            self._snapshot_group_for_room(final, household_id, room_uid) or {}
        )
        source_members = set(source_group.get("memberUids", []))
        destination_members = set(destination_group.get("memberUids", []))
        source_state = str(source_group.get("playbackState", "")).upper()

        if source_members != {source_uid} or destination_members != {room_uid}:
            self._rollback_audio_handoff(
                source,
                destination,
                source_uid,
                room_uid,
                household_id,
                source_was_playing,
            )
            raise ControllerError(
                "Sonos partially moved the audio; the rooms are not standalone"
            )

        # Coordinator changes can restart the detached source. If that
        # happened, pause it again after topology confirms it is independent,
        # then resume only the new destination.
        if source_was_playing:
            if source_state == "PLAYING":
                source.pause()
            self._play_confirmed_coordinator(destination)

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
            refreshed = self._refresh_after_topology_mutation(target["householdId"])
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
            self._refresh_after_topology_mutation(target["householdId"])

        # Coordinator removal is deliberately last because Sonos elects the
        # replacement. Anchor to a retained room before the topology shifts.
        if old_coordinator_uid not in requested_set:
            self.state.selected_room_uid = retained_uid
            self._save_state_quietly()
            detached = self._zone(old_coordinator_uid)
            detached.unjoin()
            final = self._refresh_after_topology_mutation(target["householdId"])

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
