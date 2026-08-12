import io
import json

from omasonos_backend.protocol import ProtocolServer


class FakeController:
    def __init__(self):
        self.calls = []

    def refresh(self, *, rediscover=True):
        self.calls.append(("refresh", rediscover))
        return {
            "type": "snapshot",
            "version": 1,
            "status": {"state": "ready", "message": ""},
            "households": [],
            "target": None,
            "playback": {},
        }

    def event_services(self):
        return {}

    def play_pause(self):
        self.calls.append(("playPause",))

    def set_group_volume(self, volume):
        self.calls.append(("setGroupVolume", volume))

    def play_favorite(self, favorite_id):
        self.calls.append(("playFavorite", favorite_id))

    def move_playback_to_room(self, room_uid):
        self.calls.append(("movePlaybackToRoom", room_uid))



def decoded(output):
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_mutation_emits_result_then_fresh_snapshot():
    controller = FakeController()
    server = ProtocolServer(controller)  # type: ignore[arg-type]
    output = io.StringIO()
    server.handle({"id": "12", "op": "playPause"}, output)
    messages = decoded(output)
    assert controller.calls == [("playPause",), ("refresh", False)]
    assert messages[0] == {"type": "result", "id": "12", "ok": True}
    assert messages[1]["type"] == "snapshot"


def test_unknown_operation_is_reported_without_crashing():
    controller = FakeController()
    server = ProtocolServer(controller)  # type: ignore[arg-type]
    output = io.StringIO()
    server.handle({"id": "99", "op": "wat"}, output)
    messages = decoded(output)
    assert messages == [
        {"type": "result", "id": "99", "ok": False, "error": "Unknown operation: wat"}
    ]


def test_set_panel_open_is_local_protocol_state():
    controller = FakeController()
    server = ProtocolServer(controller)  # type: ignore[arg-type]
    output = io.StringIO()
    server.handle({"id": "2", "op": "setPanelOpen", "open": True}, output)
    assert server.panel_open is True
    assert decoded(output)[0]["ok"] is True


def test_plan_style_top_level_arguments_are_used():
    controller = FakeController()
    server = ProtocolServer(controller)  # type: ignore[arg-type]
    output = io.StringIO()
    server.handle({"id": "3", "op": "setGroupVolume", "volume": 35}, output)
    assert controller.calls == [("setGroupVolume", 35), ("refresh", False)]
    assert decoded(output)[0]["ok"] is True


def test_play_favorite_dispatches_opaque_id():
    controller = FakeController()
    server = ProtocolServer(controller)  # type: ignore[arg-type]
    output = io.StringIO()
    server.handle({"id": "4", "op": "playFavorite", "favoriteId": "fav-1"}, output)
    assert controller.calls == [("playFavorite", "fav-1"), ("refresh", False)]
    assert decoded(output)[0]["ok"] is True


def test_move_playback_dispatches_dedicated_room_operation():
    controller = FakeController()
    server = ProtocolServer(controller)  # type: ignore[arg-type]
    output = io.StringIO()
    server.handle(
        {"id": "5", "op": "movePlaybackToRoom", "roomUid": "R2"}, output
    )
    assert controller.calls == [("movePlaybackToRoom", "R2"), ("refresh", False)]
    assert decoded(output)[0]["ok"] is True


def test_refresh_exception_becomes_error_snapshot():
    class BrokenController(FakeController):
        def refresh(self, *, rediscover=True):
            raise RuntimeError("network exploded")

    server = ProtocolServer(BrokenController())  # type: ignore[arg-type]
    output = io.StringIO()
    server.emit_snapshot(output)
    message = decoded(output)[0]
    assert message["type"] == "snapshot"
    assert message["status"]["state"] == "error"
    assert "network exploded" in message["status"]["message"]
