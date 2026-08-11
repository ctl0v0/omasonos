from dataclasses import dataclass

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
        discover_fn=lambda timeout=2: {living, kitchen},
        soco_factory=lambda host: living,
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


def test_group_volume_and_seek(tmp_path):
    controller, living, kitchen, group = make_controller(tmp_path)
    controller.refresh()
    controller.set_group_volume(55)
    assert group.volume == 55
    controller.seek(95)
    assert living.seek_position == "00:01:35"


def test_grouping_rejects_cross_household_members_before_mutating():
    state = PersistentState(selected_room_uid="R1", cached_hosts=[])
    state.save = lambda path=None: None  # type: ignore[method-assign]
    controller = SonosController(
        discover_fn=lambda timeout=2: set(),
        soco_factory=lambda host: None,
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
    controller.refresh = lambda: snapshot  # type: ignore[method-assign]
    with pytest.raises(ControllerError, match="outside the active Sonos household"):
        controller.apply_members(["R1", "R9"])
