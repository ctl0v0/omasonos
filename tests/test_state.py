import json

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
