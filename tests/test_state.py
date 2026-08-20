import json
import stat

from omasonos_backend.state import PersistentState


def test_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = PersistentState("RINCON_A", ["10.0.0.3", "10.0.0.2", "10.0.0.2"])
    state.save(path)
    loaded = PersistentState.load(path)
    assert loaded.selected_room_uid == "RINCON_A"
    assert loaded.cached_hosts == ["10.0.0.2", "10.0.0.3"]
    raw = json.loads(path.read_text())
    assert raw["selectedRoomUid"] == "RINCON_A"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_non_object_state_is_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[]")

    assert PersistentState.load(path) == PersistentState()
