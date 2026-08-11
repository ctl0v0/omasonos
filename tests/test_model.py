from omasonos_backend.model import (
    choose_target_group,
    clamp_volume,
    format_sonos_time,
    parse_sonos_time,
)


def test_parse_sonos_time():
    assert parse_sonos_time("01:02:03") == 3723
    assert parse_sonos_time("02:03") == 123
    assert parse_sonos_time("NOT_IMPLEMENTED") is None
    assert parse_sonos_time("") is None
    assert parse_sonos_time("1:99:00") is None


def test_format_sonos_time():
    assert format_sonos_time(95) == "00:01:35"
    assert format_sonos_time(-3) == "00:00:00"


def test_clamp_volume():
    assert clamp_volume(-1) == 0
    assert clamp_volume(40.6) == 41
    assert clamp_volume(101) == 100


def test_choose_target_prefers_persistent_anchor():
    groups = [
        {"uid": "a", "memberUids": ["R1"], "playbackState": "PLAYING"},
        {"uid": "b", "memberUids": ["R2", "R3"], "playbackState": "PAUSED_PLAYBACK"},
    ]
    assert choose_target_group(groups, "R3")["uid"] == "b"


def test_choose_target_falls_back_to_playing_then_first():
    groups = [
        {"uid": "a", "memberUids": ["R1"], "playbackState": "STOPPED"},
        {"uid": "b", "memberUids": ["R2"], "playbackState": "PLAYING"},
    ]
    assert choose_target_group(groups, "missing")["uid"] == "b"
    groups[1]["playbackState"] = "PAUSED_PLAYBACK"
    assert choose_target_group(groups, "missing")["uid"] == "a"
