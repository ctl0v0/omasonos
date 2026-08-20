from dataclasses import dataclass
import xml.etree.ElementTree as ET

import pytest

from omasonos_backend.controller import ControllerError, SonosController
from omasonos_backend.state import PersistentState


@dataclass
class FakeTransport:
    state: str = "STOPPED"


class FakeGroup:
    def __init__(self, coordinator, members, volume=35, mute=False):
        self.coordinator = coordinator
        self.members = set(members)
        self.volume = volume
        self.mute = mute

    def set_relative_volume(self, delta):
        self.volume += delta


class FakeAVTransport:
    def __init__(self):
        self.media_info = {"CurrentURI": "", "CurrentURIMetaData": ""}
        self.error = None

    def GetMediaInfo(self, args):
        self.args = args
        if self.error is not None:
            raise self.error
        return self.media_info

    def SetAVTransportURI(self, args):
        self.set_uri_args = args

    def Seek(self, args):
        self.seek_args = args

    def Play(self, args):
        self.play_args = args


class FakeZone:
    def __init__(self, uid, name, ip, household="HH1"):
        self.uid = uid
        self.player_name = name
        self.ip_address = ip
        self.household_id = household
        self.volume = 25
        self.mute = False
        self._all_groups = []
        self._visible_zones = set()
        self._transport = "STOPPED"
        self.group = None
        self.available_actions = ["Set", "Play", "Pause", "Next"]
        self.music_source = "SPOTIFY_CONNECT"
        self.music_library = FakeMusicLibrary()
        self.avTransport = FakeAVTransport()

    @property
    def visible_zones(self):
        return self._visible_zones

    @property
    def all_groups(self):
        return set(self._all_groups)

    def get_current_transport_info(self):
        return {"current_transport_state": self._transport}

    def get_current_track_info(self):
        return {
            "title": "Track",
            "artist": "Artist",
            "album": "Album",
            "position": "00:01:05",
            "duration": "00:03:00",
            "album_art": "/getaa?s=1&u=x",
        }

    def play(self):
        self._transport = "PLAYING"

    def pause(self):
        self._transport = "PAUSED_PLAYBACK"

    def next(self):
        pass

    def previous(self):
        pass

    def seek(self, position):
        self.seek_position = position

    def set_relative_volume(self, delta):
        self.volume += delta

    def play_uri(self, **kwargs):
        self.played_uri = kwargs

    def add_to_queue(self, item):
        self.queued_item = item
        return 4

    def play_from_queue(self, index):
        self.played_queue_index = index



class FakeSearchResult(list):
    total_matches = 0


class FakeMusicLibrary:
    def __init__(self, favorites=None, total=None):
        self.favorites = favorites or []
        self.total = len(self.favorites) if total is None else total

    def get_sonos_favorites(self, **kwargs):
        result = FakeSearchResult(self.favorites)
        result.total_matches = self.total
        self.kwargs = kwargs
        return result


class FakeResource:
    def __init__(self, uri):
        self.uri = uri


class FakeReference:
    def __init__(self, item_id="", desc="", uri=""):
        self.item_id = item_id
        self.desc = desc
        self.resources = [FakeResource(uri)] if uri else []


class FakeFavorite:
    def __init__(self, title, uri="", metadata="<DIDL-Lite />", reference=None):
        self.title = title
        self.resources = [FakeResource(uri)] if uri else []
        self.resource_meta_data = metadata
        self.reference = reference


def make_controller(tmp_path):
    living = FakeZone("R1", "Living Room", "10.0.0.2")
    kitchen = FakeZone("R2", "Kitchen", "10.0.0.3")
    group = FakeGroup(living, [living, kitchen], volume=42)
    living.group = kitchen.group = group
    living._all_groups = kitchen._all_groups = [group]
    living._visible_zones = kitchen._visible_zones = {living, kitchen}
    state = PersistentState(selected_room_uid="R2", cached_hosts=[])
    # Avoid touching real XDG state in unit tests.
    state.save = lambda path=None: None  # type: ignore[method-assign]
    controller = SonosController(
        discover_fn=lambda **kwargs: {living, kitchen},
        soco_factory=lambda host: living,
        network_scan_fn=lambda **kwargs: set(),
        persistent_state=state,
    )
    return controller, living, kitchen, group


def test_refresh_builds_target_and_playback(tmp_path):
    controller, living, kitchen, group = make_controller(tmp_path)
    living._transport = "PLAYING"
    snapshot = controller.refresh()
    assert snapshot["status"]["state"] == "ready"
    assert snapshot["target"]["memberUids"] == ["R1", "R2"] or set(snapshot["target"]["memberUids"]) == {"R1", "R2"}
    assert snapshot["target"]["volume"] == 42
    assert snapshot["playback"]["title"] == "Track"
    assert snapshot["playback"]["positionSec"] == 65
    assert snapshot["playback"]["artworkUrl"].startswith("http://10.0.0.2:1400/")
    room_states = {
        room["name"]: room["playbackState"]
        for household in snapshot["households"]
        for room in household["rooms"]
    }
    assert room_states == {"Kitchen": "PLAYING", "Living Room": "PLAYING"}


def test_radio_uses_media_metadata_when_track_metadata_is_blank(tmp_path):
    controller, living, _, _ = make_controller(tmp_path)
    living._transport = "PLAYING"
    living.music_source = "RADIO"
    living.get_current_track_info = lambda: {
        "title": "",
        "artist": "",
        "album": "",
        "album_art": "",
        "position": "00:02:10",
        "duration": "00:00:00",
    }
    living.avTransport.media_info = {
        "CurrentURI": "x-sonosapi-stream:station",
        "CurrentURIMetaData": (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            "<item><dc:title>Connecticut Public Radio</dc:title>"
            "<upnp:albumArtURI>https://example.test/station.png</upnp:albumArtURI>"
            "</item></DIDL-Lite>"
        ),
    }

    snapshot = controller.refresh()

    assert snapshot["playback"]["state"] == "PLAYING"
    assert snapshot["playback"]["title"] == "Connecticut Public Radio"
    assert snapshot["playback"]["artworkUrl"] == "https://example.test/station.png"
    assert snapshot["playback"]["metadataState"] == "fresh"


def test_complete_media_title_replaces_truncated_track_title(tmp_path):
    controller, living, _, _ = make_controller(tmp_path)
    living._transport = "PLAYING"
    living.get_current_track_info = lambda: {
        "title": "Luigi Mangione’s High-Risk L",
        "artist": "The New York Times",
        "album": "The Daily",
        "album_art": "",
        "position": "00:08:42",
        "duration": "00:26:39",
    }
    living.avTransport.media_info = {
        "CurrentURI": "https://example.test/daily.mp3",
        "CurrentURIMetaData": (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<item><dc:title>Luigi Mangione’s High-Risk Legal Strategy</dc:title>"
            "</item></DIDL-Lite>"
        ),
    }

    snapshot = controller.refresh()

    assert snapshot["playback"]["title"] == (
        "Luigi Mangione’s High-Risk Legal Strategy"
    )


def test_transient_metadata_failure_keeps_last_confirmed_track(tmp_path):
    controller, living, _, _ = make_controller(tmp_path)
    living._transport = "PLAYING"
    first = controller.refresh()
    assert first["playback"]["title"] == "Track"

    def fail_track_info():
        raise OSError("speaker busy")

    living.get_current_track_info = fail_track_info
    living.avTransport.error = OSError("media query timed out")
    recovered = controller.refresh(rediscover=False)

    assert recovered["playback"]["state"] == "PLAYING"
    assert recovered["playback"]["title"] == "Track"
    assert recovered["playback"]["artworkUrl"].endswith("/getaa?s=1&u=x")
    assert recovered["playback"]["metadataState"] == "cached"
    assert recovered["playback"]["stale"] is True
    assert recovered["status"]["playbackDegraded"] is True


def test_confirmed_stop_clears_previous_playback_metadata(tmp_path):
    controller, living, _, _ = make_controller(tmp_path)
    living._transport = "PLAYING"
    controller.refresh()

    living._transport = "STOPPED"
    stopped = controller.refresh(rediscover=False)

    assert stopped["playback"]["state"] == "STOPPED"
    assert stopped["playback"]["title"] == ""
    assert stopped["playback"]["artworkUrl"] == ""
    assert stopped["playback"]["metadataState"] == "empty"


def test_no_speakers_is_an_expected_offline_state():
    state = PersistentState(selected_room_uid="", cached_hosts=[])
    state.save = lambda path=None: None  # type: ignore[method-assign]
    controller = SonosController(
        discover_fn=lambda **kwargs: set(),
        soco_factory=lambda host: None,
        network_scan_fn=lambda **kwargs: set(),
        persistent_state=state,
    )

    snapshot = controller.refresh()

    assert snapshot["status"]["state"] == "offline"
    assert snapshot["status"]["message"] == "Not connected to your Sonos network"
    assert snapshot["target"] is None


def test_group_volume_and_seek(tmp_path):
    controller, living, kitchen, group = make_controller(tmp_path)
    controller.refresh()
    controller.set_group_volume(55)
    assert group.volume == 55
    controller.seek(95)
    assert living.seek_position == "00:01:35"


def test_event_services_include_transport_for_every_playback_group(tmp_path):
    controller, living, kitchen, selected_group = make_controller(tmp_path)
    office = FakeZone("R3", "Office", "10.0.0.4")
    office_group = FakeGroup(office, [office])
    office.group = office_group
    living.avTransport = object()
    office.avTransport = object()
    controller._zones = {"R1": living, "R2": kitchen, "R3": office}
    controller._target_group = selected_group

    services = controller.event_services()

    assert services["transport:R1"] is living.avTransport
    assert services["transport:R3"] is office.avTransport
    assert len([key for key in services if key.startswith("transport:")]) == 2


def test_room_name_removes_leading_invisible_formatting_marks():
    office = FakeZone("R1", "\ufe0f Office", "10.0.0.2")
    dining = FakeZone("R2", "\u200bDining Room", "10.0.0.3")

    assert SonosController._zone_name(office) == "Office"
    assert SonosController._zone_name(dining) == "Dining Room"


def test_favorites_expose_only_directly_playable_items(tmp_path):
    controller, living, _, _ = make_controller(tmp_path)
    living.music_library = FakeMusicLibrary(
        [
            FakeFavorite("Station", "x-sonosapi-stream:123"),
            FakeFavorite("Saved album", "x-rincon-cpcontainer:album-1"),
            FakeFavorite("Playlist container"),
            FakeFavorite("Missing metadata", "x-sonos-http:track", ""),
        ]
    )

    snapshot = controller.refresh()

    assert snapshot["favorites"]["state"] == "ready"
    assert snapshot["favorites"]["total"] == 4
    assert snapshot["favorites"]["unsupported"] == 3
    assert [item["title"] for item in snapshot["favorites"]["items"]] == [
        "Station",
    ]
    assert snapshot["favorites"]["items"][0]["kind"] == "radio"
    assert living.music_library.kwargs == {
        "complete_result": True,
        "max_items": 100,
    }


def test_play_favorite_uses_cached_uri_and_metadata(tmp_path):
    controller, living, _, _ = make_controller(tmp_path)
    living.music_library = FakeMusicLibrary(
        [FakeFavorite("Saved track", "x-sonos-http:track", "<meta />")]
    )
    snapshot = controller.refresh()
    favorite_id = snapshot["favorites"]["items"][0]["id"]

    controller.play_favorite(favorite_id)

    assert living.played_uri == {
        "uri": "x-sonos-http:track",
        "meta": "<meta />",
        "start": True,
    }


def test_queueable_container_favorite_is_added_and_played(tmp_path):
    controller, living, _, _ = make_controller(tmp_path)
    reference = FakeReference(uri="x-rincon-cpcontainer:album-1")
    living.music_library = FakeMusicLibrary(
        [
            FakeFavorite(
                "Saved album",
                "x-rincon-cpcontainer:album-1",
                reference=reference,
            )
        ]
    )
    snapshot = controller.refresh()
    favorite_id = snapshot["favorites"]["items"][0]["id"]

    controller.play_favorite(favorite_id)

    assert living.queued_item is reference
    assert living.played_queue_index == 3


def test_tunein_podcast_favorite_plays_latest_episode_without_developer_account(
    tmp_path,
):
    controller, living, _, _ = make_controller(tmp_path)
    reference = FakeReference(
        item_id=(
            "100b2064p295446%3Atopic--"
            "2e8792e144a1455a82a2c35380306c07"
        ),
        desc="SA_RINCON85255_X_#Svc85255-0-Token",
    )
    living.music_library = FakeMusicLibrary(
        [FakeFavorite("Stuff You Should Know", reference=reference)]
    )
    episode = type(
        "FakeEpisode",
        (),
        {
            "id": "episode-1",
            "title": "The Manhattan Grid",
            "desc": "SA_RINCON65031_",
            "metadata": {
                "track_metadata": type(
                    "FakeTrackMetadata",
                    (),
                    {
                        "metadata": {
                            "podcast": "Stuff You Should Know",
                            "host": "Josh and Chuck",
                            "duration": 2852,
                            "album_art_uri": (
                                "https://cdn.example.test/sysk.png?version=1"
                            ),
                        }
                    },
                )()
            },
        },
    )()

    class FakeTuneIn:
        def get_metadata(self, item_id, count):
            self.request = (item_id, count)
            return [episode]

    tunein = FakeTuneIn()
    controller._tunein_service = lambda coordinator: tunein
    controller._tunein_media_url = lambda service, item: (
        "https://podcast.example.test/episode.mp3"
    )
    snapshot = controller.refresh()
    favorite = snapshot["favorites"]["items"][0]

    controller.play_favorite(favorite["id"])

    assert favorite["kind"] == "podcast"
    assert tunein.request == (
        "p295446:topic--2e8792e144a1455a82a2c35380306c07",
        1,
    )
    assert living.played_uri["uri"] == "https://podcast.example.test/episode.mp3"
    assert living.played_uri["start"] is True
    metadata = ET.fromstring(living.played_uri["meta"])
    assert metadata.findtext(".//{http://purl.org/dc/elements/1.1/}title") == (
        "The Manhattan Grid"
    )
    assert metadata.findtext(
        ".//{urn:schemas-upnp-org:metadata-1-0/upnp/}albumArtURI"
    ) == "https://cdn.example.test/sysk.png?version=1"
    assert metadata.findtext(
        ".//{urn:schemas-upnp-org:metadata-1-0/upnp/}album"
    ) == "Stuff You Should Know"
    assert metadata.findtext(".//{http://purl.org/dc/elements/1.1/}creator") == (
        "Josh and Chuck"
    )


def test_unknown_favorite_id_is_rejected(tmp_path):
    controller, _, _, _ = make_controller(tmp_path)
    controller.refresh()

    with pytest.raises(ControllerError, match="Unknown or unavailable"):
        controller.play_favorite("not-a-real-id")


def test_grouping_rejects_cross_household_members_before_mutating():
    state = PersistentState(selected_room_uid="R1", cached_hosts=[])
    state.save = lambda path=None: None  # type: ignore[method-assign]
    controller = SonosController(
        discover_fn=lambda **kwargs: set(),
        soco_factory=lambda host: None,
        network_scan_fn=lambda **kwargs: set(),
        persistent_state=state,
    )
    snapshot = {
        "target": {
            "householdId": "HH1",
            "coordinatorUid": "R1",
            "memberUids": ["R1"],
        },
        "households": [
            {"id": "HH1", "rooms": [{"uid": "R1"}], "groups": []},
            {"id": "HH2", "rooms": [{"uid": "R9"}], "groups": []},
        ],
    }
    controller.refresh = lambda **kwargs: snapshot  # type: ignore[method-assign]
    with pytest.raises(ControllerError, match="outside the active Sonos household"):
        controller.apply_members(["R1", "R9"])


def test_move_rejects_room_in_another_group_without_mutating():
    controller, living, kitchen, _ = make_controller(None)
    office = FakeZone("R3", "Office", "10.0.0.4")
    snapshot = {
        "target": {
            "householdId": "HH1",
            "groupUid": "R1",
            "coordinatorUid": "R1",
            "memberUids": ["R1"],
        },
        "playback": {"state": "PLAYING"},
        "households": [
            {
                "id": "HH1",
                "rooms": [{"uid": "R1"}, {"uid": "R2"}, {"uid": "R3"}],
                "groups": [
                    {"uid": "R1", "memberUids": ["R1"]},
                    {"uid": "R2", "memberUids": ["R2", "R3"]},
                ],
            }
        ],
    }
    controller.refresh = lambda **kwargs: snapshot  # type: ignore[method-assign]
    controller.apply_members = lambda members: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("must not mutate topology")
    )

    with pytest.raises(ControllerError, match="belongs to another group"):
        controller.move_playback_to_room("R2")


def test_move_rejects_current_multi_room_group_without_mutating():
    controller, _, _, _ = make_controller(None)
    snapshot = {
        "target": {
            "householdId": "HH1",
            "groupUid": "R1",
            "coordinatorUid": "R1",
            "memberUids": ["R1", "R2"],
        },
        "playback": {"state": "PLAYING"},
        "households": [
            {
                "id": "HH1",
                "rooms": [{"uid": "R1"}, {"uid": "R2"}, {"uid": "R3"}],
                "groups": [
                    {"uid": "R1", "memberUids": ["R1", "R2"]},
                    {"uid": "R3", "memberUids": ["R3"]},
                ],
            }
        ],
    }
    controller.refresh = lambda **kwargs: snapshot  # type: ignore[method-assign]
    controller.apply_members = lambda members: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("must not mutate topology")
    )

    with pytest.raises(ControllerError, match="playing on a group"):
        controller.move_playback_to_room("R3")


@pytest.mark.parametrize("playback_state", ["STOPPED", "PAUSED_PLAYBACK"])
def test_move_without_playing_audio_only_changes_active_room(playback_state):
    controller, living, kitchen, _ = make_controller(None)
    office = FakeZone("R3", "Office", "10.0.0.4")
    snapshot = {
        "target": {
            "householdId": "HH1",
            "groupUid": "R1",
            "coordinatorUid": "R1",
            "memberUids": ["R1"],
        },
        "playback": {"state": playback_state},
        "households": [
            {
                "id": "HH1",
                "rooms": [{"uid": "R1"}, {"uid": "R2"}, {"uid": "R3"}],
                "groups": [
                    {"uid": "R1", "memberUids": ["R1"]},
                    {"uid": "R2", "memberUids": ["R2", "R3"]},
                ],
            }
        ],
    }
    controller.refresh = lambda **kwargs: snapshot  # type: ignore[method-assign]
    living.pause = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("must not pause or mutate playback")
    )
    kitchen.join = lambda source: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("must not join rooms")
    )
    office.join = kitchen.join  # type: ignore[attr-defined]

    controller.move_playback_to_room("R2")

    assert controller.state.selected_room_uid == "R2"


def test_move_to_standalone_room_waits_for_topology_then_plays_destination(
    monkeypatch,
):
    kitchen = FakeZone("R1", "Kitchen", "10.0.0.2")
    office = FakeZone("R2", "Office", "10.0.0.3")
    events = []
    kitchen.pause = lambda: events.append(("pause", "R1"))
    kitchen.play = lambda: events.append(("play", "R1"))
    kitchen.unjoin = lambda: events.append(("unjoin", "R1"))
    office.join = lambda coordinator: events.append(
        ("join", "R2", coordinator.uid)
    )
    office.play = lambda: events.append(("play", "R2"))

    state = PersistentState(selected_room_uid="R1", cached_hosts=[])
    state.save = lambda path=None: None  # type: ignore[method-assign]
    controller = SonosController(
        discover_fn=lambda **kwargs: {kitchen, office},
        soco_factory=lambda host: kitchen,
        network_scan_fn=lambda **kwargs: set(),
        persistent_state=state,
    )
    controller._zones = {"R1": kitchen, "R2": office}
    cache_clears = []
    kitchen.zone_group_state = type(
        "FakeTopologyCache",
        (),
        {"clear_cache": lambda self: cache_clears.append("R1")},
    )()
    office.zone_group_state = type(
        "FakeTopologyCache",
        (),
        {"clear_cache": lambda self: cache_clears.append("R2")},
    )()

    def snapshot(target_members, groups, playback_state):
        return {
            "target": {
                "householdId": "HH1",
                "coordinatorUid": target_members[0],
                "memberUids": target_members,
            },
            "playback": {"state": playback_state},
            "households": [
                {
                    "id": "HH1",
                    "rooms": [{"uid": "R1"}, {"uid": "R2"}],
                    "groups": groups,
                }
            ],
        }

    initial = snapshot(
        ["R1"],
        [
            {
                "coordinatorUid": "R1",
                "memberUids": ["R1"],
                "playbackState": "PLAYING",
            },
            {"coordinatorUid": "R2", "memberUids": ["R2"]},
        ],
        "PLAYING",
    )
    joined = snapshot(
        ["R1", "R2"],
        [{"coordinatorUid": "R1", "memberUids": ["R1", "R2"]}],
        "PAUSED_PLAYBACK",
    )
    moved = snapshot(
        ["R2"],
        [
            {
                "coordinatorUid": "R1",
                "memberUids": ["R1"],
                "playbackState": "PAUSED_PLAYBACK",
            },
            {"coordinatorUid": "R2", "memberUids": ["R2"]},
        ],
        "PAUSED_PLAYBACK",
    )
    # Each topology mutation first returns a stale cached view. The handoff
    # must retry instead of reporting a false failure on that first snapshot.
    snapshots = iter([initial, initial, joined, joined, moved, moved])
    controller.refresh = lambda **kwargs: next(snapshots)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "omasonos_backend.controller.TOPOLOGY_SETTLE_INTERVAL_SEC", 0
    )

    controller.move_playback_to_room("R2")

    assert events == [
        ("pause", "R1"),
        ("join", "R2", "R1"),
        ("unjoin", "R1"),
        ("play", "R2"),
    ]
    assert state.selected_room_uid == "R2"
    assert cache_clears == ["R1", "R2"] * 4


def test_direct_stream_handoff_avoids_grouping_and_preserves_position():
    kitchen = FakeZone("R1", "Kitchen", "10.0.0.2")
    office = FakeZone("R2", "Office", "10.0.0.3")
    kitchen.avTransport.media_info = {
        "CurrentURI": "https://podcast.example.test/daily.mp3",
        "CurrentURIMetaData": "<DIDL-Lite />",
    }
    state = PersistentState(selected_room_uid="R1", cached_hosts=[])
    state.save = lambda path=None: None  # type: ignore[method-assign]
    controller = SonosController(
        discover_fn=lambda **kwargs: {kitchen, office},
        soco_factory=lambda host: kitchen,
        network_scan_fn=lambda **kwargs: set(),
        persistent_state=state,
    )
    controller._zones = {"R1": kitchen, "R2": office}
    snapshot = {
        "target": {
            "householdId": "HH1",
            "coordinatorUid": "R1",
            "memberUids": ["R1"],
        },
        "playback": {
            "state": "PLAYING",
            "positionSec": 522,
            "availableActions": ["Play", "Pause", "SeekTime"],
        },
        "households": [
            {
                "id": "HH1",
                "rooms": [{"uid": "R1"}, {"uid": "R2"}],
                "groups": [
                    {"coordinatorUid": "R1", "memberUids": ["R1"]},
                    {"coordinatorUid": "R2", "memberUids": ["R2"]},
                ],
            }
        ],
    }
    controller.refresh = lambda **kwargs: snapshot  # type: ignore[method-assign]

    controller.move_playback_to_room("R2")

    assert kitchen._transport == "PAUSED_PLAYBACK"
    assert office._transport == "PLAYING"
    assert office.avTransport.set_uri_args == [
        ("InstanceID", 0),
        ("CurrentURI", "https://podcast.example.test/daily.mp3"),
        ("CurrentURIMetaData", "<DIDL-Lite />"),
    ]
    assert office.avTransport.seek_args == [
        ("InstanceID", 0),
        ("Unit", "REL_TIME"),
        ("Target", "00:08:42"),
    ]
    assert state.selected_room_uid == "R2"


def test_authoritative_topology_refresh_bypasses_subscription_cache():
    living = FakeZone("R1", "Living Room", "10.0.0.2")
    processed = []

    class FakeTopologyService:
        def GetZoneGroupState(self, **kwargs):
            assert kwargs == {"timeout": 2.0}
            return {"ZoneGroupState": "<ZoneGroups />"}

    class FakeTopologyState:
        has_subscriptions = True

        def process_payload(self, **kwargs):
            processed.append(kwargs)

    living.zoneGroupTopology = FakeTopologyService()
    living.zone_group_state = FakeTopologyState()
    controller = SonosController(
        discover_fn=lambda **kwargs: set(),
        soco_factory=lambda host: living,
        network_scan_fn=lambda **kwargs: set(),
        persistent_state=PersistentState(),
    )
    controller._zones = {"R1": living}

    assert controller._refresh_topology_authoritatively() is True
    assert processed == [
        {
            "payload": "<ZoneGroups />",
            "source": "omasonos-authoritative-poll",
            "source_ip": "10.0.0.2",
        }
    ]


def test_authoritative_topology_refresh_targets_requested_household():
    first = FakeZone("R1", "Living Room", "10.0.0.2", household="HH1")
    second = FakeZone("R2", "Office", "10.0.1.2", household="HH2")
    called = []

    class FakeTopologyService:
        def __init__(self, household):
            self.household = household

        def GetZoneGroupState(self, **kwargs):
            called.append(self.household)
            return {"ZoneGroupState": "<ZoneGroups />"}

    class FakeTopologyState:
        def process_payload(self, **kwargs):
            pass

    for zone in (first, second):
        zone.zoneGroupTopology = FakeTopologyService(zone.household_id)
        zone.zone_group_state = FakeTopologyState()
    controller = SonosController(
        discover_fn=lambda **kwargs: set(),
        soco_factory=lambda host: first,
        network_scan_fn=lambda **kwargs: set(),
        persistent_state=PersistentState(),
    )
    controller._zones = {"R1": first, "R2": second}

    controller.refresh_event_topologies({"HH2"})

    assert called == ["HH2"]


def test_failed_handoff_verification_restores_original_playback(monkeypatch):
    kitchen = FakeZone("R1", "Kitchen", "10.0.0.2")
    office = FakeZone("R2", "Office", "10.0.0.3")
    events = []
    kitchen.pause = lambda: events.append(("pause", "R1"))
    kitchen.play = lambda: events.append(("play", "R1"))
    office.join = lambda coordinator: events.append(("join", "R2", coordinator.uid))
    office.unjoin = lambda: events.append(("unjoin", "R2"))
    state = PersistentState(selected_room_uid="R1", cached_hosts=[])
    state.save = lambda path=None: None  # type: ignore[method-assign]
    controller = SonosController(
        discover_fn=lambda **kwargs: {kitchen, office},
        soco_factory=lambda host: kitchen,
        network_scan_fn=lambda **kwargs: set(),
        persistent_state=state,
    )
    controller._zones = {"R1": kitchen, "R2": office}

    standalone = {
        "target": {
            "householdId": "HH1",
            "coordinatorUid": "R1",
            "memberUids": ["R1"],
        },
        "playback": {"state": "PLAYING"},
        "households": [
            {
                "id": "HH1",
                "rooms": [{"uid": "R1"}, {"uid": "R2"}],
                "groups": [
                    {"coordinatorUid": "R1", "memberUids": ["R1"]},
                    {"coordinatorUid": "R2", "memberUids": ["R2"]},
                ],
            }
        ],
    }
    snapshots = iter([standalone, standalone, standalone, standalone])
    controller.refresh = lambda **kwargs: next(snapshots)  # type: ignore[method-assign]
    monkeypatch.setattr("omasonos_backend.controller.TOPOLOGY_SETTLE_ATTEMPTS", 2)
    monkeypatch.setattr("omasonos_backend.controller.TOPOLOGY_SETTLE_INTERVAL_SEC", 0)

    with pytest.raises(ControllerError, match="did not prepare"):
        controller.move_playback_to_room("R2")

    assert events == [
        ("pause", "R1"),
        ("join", "R2", "R1"),
        ("unjoin", "R2"),
        ("play", "R1"),
    ]
    assert state.selected_room_uid == "R1"


def test_apply_members_joins_destination_then_detaches_and_stops_old_coordinator():
    living = FakeZone("R1", "Living Room", "10.0.0.2")
    kitchen = FakeZone("R2", "Kitchen", "10.0.0.3")
    events = []
    kitchen.join = lambda coordinator: events.append(("join", "R2", coordinator.uid))
    living.unjoin = lambda: events.append(("unjoin", "R1"))
    living.stop = lambda: events.append(("stop", "R1"))

    state = PersistentState(selected_room_uid="R1", cached_hosts=[])
    state.save = lambda path=None: None  # type: ignore[method-assign]
    controller = SonosController(
        discover_fn=lambda **kwargs: {living, kitchen},
        soco_factory=lambda host: living,
        network_scan_fn=lambda **kwargs: set(),
        persistent_state=state,
    )
    controller._zones = {"R1": living, "R2": kitchen}

    def snapshot(target_members, groups):
        return {
            "target": {
                "householdId": "HH1",
                "coordinatorUid": target_members[0],
                "memberUids": target_members,
            },
            "households": [
                {
                    "id": "HH1",
                    "rooms": [{"uid": "R1"}, {"uid": "R2"}],
                    "groups": groups,
                }
            ],
        }

    initial = snapshot(
        ["R1"],
        [
            {"coordinatorUid": "R1", "memberUids": ["R1"]},
            {"coordinatorUid": "R2", "memberUids": ["R2"]},
        ],
    )
    joined = snapshot(
        ["R1", "R2"],
        [{"coordinatorUid": "R1", "memberUids": ["R1", "R2"]}],
    )
    moved = snapshot(
        ["R2"],
        [
            {"coordinatorUid": "R1", "memberUids": ["R1"]},
            {"coordinatorUid": "R2", "memberUids": ["R2"]},
        ],
    )
    snapshots = iter([initial, joined, joined, moved, moved])
    controller.refresh = lambda **kwargs: next(snapshots)  # type: ignore[method-assign]

    controller.apply_members(["R2"])

    assert events == [
        ("join", "R2", "R1"),
        ("unjoin", "R1"),
        ("stop", "R1"),
    ]
    assert state.selected_room_uid == "R2"


def test_move_playback_reports_partial_topology_instead_of_claiming_success():
    controller, living, kitchen, _ = make_controller(None)
    living.unjoin = lambda: None
    controller._zones = {"R1": living, "R2": kitchen}
    snapshot = {
        "target": {
            "householdId": "HH1",
            "coordinatorUid": "R1",
            "memberUids": ["R1", "R2"],
        },
        "households": [
            {
                "id": "HH1",
                "rooms": [{"uid": "R1"}, {"uid": "R2"}],
                "groups": [
                    {"coordinatorUid": "R1", "memberUids": ["R1", "R2"]}
                ],
            }
        ],
    }
    controller.refresh = lambda **kwargs: snapshot  # type: ignore[method-assign]

    with pytest.raises(ControllerError, match="partially applied"):
        controller.apply_members(["R2"])


def test_discovery_uses_five_second_ssdp_then_network_scan_fallback():
    living = FakeZone("R1", "Living Room", "10.0.0.2")
    living._visible_zones = {living}
    calls = []
    state = PersistentState(selected_room_uid="", cached_hosts=[])
    state.save = lambda path=None: None  # type: ignore[method-assign]

    def discover(**kwargs):
        calls.append(("ssdp", kwargs))
        return set()

    def scan_network(**kwargs):
        calls.append(("scan", kwargs))
        return {living}

    controller = SonosController(
        discover_fn=discover,
        soco_factory=lambda host: living,
        network_scan_fn=scan_network,
        persistent_state=state,
    )

    zones = controller._discover_zones()
    assert zones == [living]
    assert calls[0] == ("ssdp", {"timeout": 5})
    assert calls[1][0] == "scan"
    assert calls[1][1]["multi_household"] is True
    assert calls[1][1]["min_netmask"] == 24
    assert calls[1][1]["scan_timeout"] == 0.6
    assert controller._discovery_diagnostics["networkScanAttempted"] is True
    assert controller._discovery_diagnostics["networkScanFound"] == 1
    assert controller._discovery_diagnostics["attempts"] == [
        {"method": "ssdp", "result": "complete", "found": 0},
        {"method": "network-scan", "result": "complete", "found": 1},
    ]


def test_discovery_skips_network_scan_when_ssdp_succeeds():
    living = FakeZone("R1", "Living Room", "10.0.0.2")
    living._visible_zones = {living}
    state = PersistentState(selected_room_uid="", cached_hosts=[])
    state.save = lambda path=None: None  # type: ignore[method-assign]
    scans = []
    controller = SonosController(
        discover_fn=lambda **kwargs: {living},
        soco_factory=lambda host: living,
        network_scan_fn=lambda **kwargs: scans.append(kwargs) or set(),
        persistent_state=state,
    )

    zones = controller._discover_zones()
    assert zones == [living]
    assert scans == []
    assert controller._discovery_diagnostics["ssdpFound"] == 1
    assert controller._discovery_diagnostics["networkScanAttempted"] is False
    assert controller._discovery_diagnostics["attempts"][-1] == {
        "method": "network-scan",
        "result": "skipped",
        "reason": "already-found",
    }


def test_discovery_skips_ssdp_wait_when_cached_host_responds():
    living = FakeZone("R1", "Living Room", "10.0.0.2")
    living._visible_zones = {living}
    state = PersistentState(selected_room_uid="", cached_hosts=["10.0.0.2"])
    state.save = lambda path=None: None  # type: ignore[method-assign]
    ssdp_calls = []
    controller = SonosController(
        discover_fn=lambda **kwargs: ssdp_calls.append(kwargs) or set(),
        soco_factory=lambda host: living,
        network_scan_fn=lambda **kwargs: set(),
        persistent_state=state,
    )

    zones = controller._discover_zones()

    assert zones == [living]
    assert ssdp_calls == []
    assert controller._discovery_diagnostics["attempts"] == [
        {"method": "cache", "target": "10.0.0.2", "result": "found"},
        {"method": "ssdp", "result": "skipped", "reason": "cache-found"},
        {
            "method": "network-scan",
            "result": "skipped",
            "reason": "already-found",
        },
    ]
