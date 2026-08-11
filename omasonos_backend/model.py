from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def parse_sonos_time(value: Any) -> int | None:
    """Convert Sonos HH:MM:SS-ish values to seconds.

    SoCo may surface empty strings or NOT_IMPLEMENTED for sources that do not
    expose seekable timing. Those intentionally become None rather than zero.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NOT_IMPLEMENTED", "NONE", "NULL"}:
        return None
    parts = text.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        nums = [int(part) for part in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None
    if len(nums) == 2:
        hours = 0
        minutes, seconds = nums
    else:
        hours, minutes, seconds = nums
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def format_sonos_time(seconds: int | float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def clamp_volume(value: Any) -> int:
    try:
        numeric = round(float(value))
    except (TypeError, ValueError):
        numeric = 0
    return max(0, min(100, numeric))


def _group_member_uids(group: dict[str, Any]) -> set[str]:
    return {str(uid) for uid in group.get("memberUids", []) if uid}


def choose_target_group(
    groups: Iterable[dict[str, Any]],
    selected_anchor_uid: str | None,
) -> dict[str, Any] | None:
    """Pick the active Sonos group with stable-room affinity.

    Priority is deliberately boring and deterministic:
      1. The group containing the remembered room UID.
      2. A group currently playing.
      3. The first available group.
    """
    group_list = list(groups)
    if not group_list:
        return None

    anchor = str(selected_anchor_uid or "")
    if anchor:
        for group in group_list:
            if anchor in _group_member_uids(group):
                return group

    for group in group_list:
        if str(group.get("playbackState", "")).upper() == "PLAYING":
            return group

    return group_list[0]


def group_label(room_names: Iterable[str]) -> str:
    names = [str(name).strip() for name in room_names if str(name).strip()]
    if not names:
        return "Sonos"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} + {names[1]}"
    return f"{names[0]} + {len(names) - 1} rooms"
