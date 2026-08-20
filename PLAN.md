# OmasOnOS Build Plan

> Historical planning document. The README, manifest, and current source are
> authoritative for the implemented plugin.

## Recommendation

Build a local-first Omarchy plugin, working name `omasonos`, using SoCo rather than the Sonos cloud API.

```text
Widget.qml
    |
Service.qml                 One instance per Omarchy shell
    |
JSON-lines subprocess
    |
sonos_service.py
    |
SoCo / local UPnP
    |
Sonos household
```

This is preferable to copying `omaspotify` directly:

- `omaspotify` can read one Spotify MPRIS player entirely from QML.
- Sonos requires discovery, persistent state, group topology, command serialization, reconnect handling, and network subscriptions.
- Omarchy supports combined `service` and `bar-widget` plugins. The service exists once, while the widget is instantiated on every monitor.
- The widget can access the service through `bar.shell.serviceFor(moduleName)`.
- The Sonos cloud API would require developer credentials, OAuth, a public HTTPS redirect, token storage, and Internet access. Local UPnP needs none of that.

The installed system is compatible: Omarchy `4.0.0`, Python `3.14.6`, and SoCo `0.31.2` supports Python 3.14.

## First Release

The selected first release includes:

- Automatic Sonos discovery with cached known speakers.
- Multiple-household detection without ever grouping across households.
- Multiple-group awareness.
- Remembered active group anchored to a stable room UID.
- Automatic fallback to a currently playing group.
- Explicit group picker when multiple groups exist.
- Now-playing title, artist, album, artwork, source, progress, and room label.
- Play, pause, previous, next, and seek when supported by the current source.
- Group volume and mute.
- Individual room volume and mute.
- Staged room grouping with an Apply action.
- Everywhere grouping.
- Offline, setup, permission, and network error states.
- Mouse, wheel, keyboard, and shell IPC controls.

Sources such as radio, TV, line-in, Spotify Direct Control, and AirPlay expose different capabilities. The plugin must read Sonos `available_actions` and disable unsupported controls rather than treating errors as generic failures.

## Interface

Bar behavior:

- Horizontal bar: Sonos-style speaker glyph plus scrolling `Title · Artist`.
- Vertical bar: glyph only.
- Dimmed but visible while offline so setup and diagnostics remain accessible.
- Left click: open the panel.
- Middle click: play or pause.
- Wheel: adjust target group volume in 5% increments.
- Tooltip: track, group name, and playback status.

Main panel:

- Compact artwork and metadata header.
- Selected group button.
- Seekable progress bar when supported.
- Previous, play/pause, and next controls.
- Group volume slider and mute.
- Room mixer with per-room volume and mute.
- Rooms button for grouping.
- Group picker for switching between independently playing groups.

Rooms view:

- Display logical Sonos rooms, not bonded subwoofers, surrounds, or the hidden side of a stereo pair.
- Stage checkboxes locally.
- Provide `Everywhere`, `Cancel`, and `Apply`.
- Show a busy indicator because grouping can exceed one second.
- Replace staged state with authoritative topology after the operation.

Keyboard model:

- `j/k` or arrows navigate.
- `h/l` adjust the focused slider.
- `Space` toggles playback.
- `n/p` changes track.
- `g` opens groups.
- `r` opens rooms.
- `m` toggles mute.
- `Escape` moves back or closes.
- `Tab` switches adjacent Omarchy panels.

The UI should use Omarchy's `KeyboardPanel`, `PanelKeyCatcher`, `CursorSurface`, `PanelSlider`, `Button`, `PanelSeparator`, `Style`, and `Color` components.

## Plugin Layout

```text
manifest.json
Service.qml
Widget.qml
sonos-backend
sonos_service.py
requirements.lock
README.md
LICENSE
preview.png
tests/
```

The manifest should declare:

```json
{
  "schemaVersion": 1,
  "kinds": ["service", "bar-widget"],
  "entryPoints": {
    "service": "Service.qml",
    "barWidget": "Widget.qml"
  },
  "barWidget": {
    "category": "Media",
    "defaultSection": "right",
    "allowMultiple": false
  }
}
```

Presentation settings such as maximum bar-label width can remain inline in `shell.json`. Backend state should not depend on widget settings because Omarchy injects inline settings into widgets, not services.

Persistent data:

- `${XDG_DATA_HOME:-~/.local/share}/<plugin-id>/venv` for the isolated environment.
- `${XDG_STATE_HOME:-~/.local/state}/<plugin-id>/state.json` for selected room UID and cached speaker addresses.
- No account credentials or Sonos cloud tokens.

## Backend

`sonos-backend` should:

- Create the isolated environment on first launch.
- Install a hash-locked SoCo `0.31.2` dependency set.
- Use a setup lock to prevent concurrent environment creation.
- Never require `sudo`.
- Keep logs on stderr and JSON protocol messages on stdout.

`sonos_service.py` should maintain an authoritative snapshot containing:

- Households.
- Logical rooms and online state.
- Groups, coordinators, and members.
- Selected anchor room and target group.
- Playback metadata and available actions.
- Group and room volume states.
- Setup, discovery, polling, and error status.

Commands should use request IDs:

```json
{"id":"12","op":"playPause"}
{"id":"13","op":"seek","positionSec":95}
{"id":"14","op":"setGroupVolume","volume":35}
{"id":"15","op":"setRoomVolume","roomUid":"RINCON_...","volume":25}
{"id":"16","op":"applyMembers","roomUids":["RINCON_...","RINCON_..."]}
```

Every mutation should produce a result followed by a fresh authoritative snapshot. Network operations should be serialized to avoid conflicting topology changes.

## Grouping Safety

SoCo's group methods are low-level:

- `room.join(coordinator)` redirects that room to the coordinator.
- `room.unjoin()` makes that room a standalone coordinator.
- Playback commands only work on the current coordinator.
- Removing a coordinator does not explicitly identify its replacement.

Use this reconciliation sequence:

1. Refresh topology and resolve all requested room UIDs.
2. Add selected rooms to the current coordinator one at a time.
3. Verify topology after every addition.
4. Remove unwanted non-coordinator rooms one at a time.
5. If the old coordinator remains selected, verify final membership and finish.
6. If the old coordinator is not selected, remove it last.
7. Rediscover the coordinator of the retained group.
8. Update the selected anchor to a retained room.
9. Stop a detached room only after confirming it is standalone.
10. Report the actual final state if Sonos partially applies the request.

Do not use SoCo `partymode()` for reconciliation. It has no transactional behavior, rollback, or reliable handling of multiple pre-existing groups.

## Live Updates

Use push events when networking permits:

- Zone group topology subscription.
- Target coordinator AVTransport subscription.
- Group volume subscription.
- Rendering control subscription for visible rooms.

Sonos must be able to connect back to the computer on TCP `1400-1499`. When subscriptions fail, automatically use polling:

- Fast playback refresh while the panel is open.
- Slower topology refresh while closed.
- Immediate refresh after every command.
- Exponential retry for offline speakers.

Track position should be extrapolated locally between authoritative updates instead of issuing a network request every second.

## AirPlay Phase

The discussion describes routing Linux system audio, which is separate from controlling audio already playing on Sonos.

Implement it as an opt-in second phase:

1. Document installation of `pipewire-zeroconf`.
2. Document the user-owned RAOP configuration under `~/.config/pipewire/pipewire.conf.d/`.
3. Detect RAOP sinks through `Quickshell.Services.Pipewire`.
4. Match sinks to Sonos rooms where possible.
5. Add a `Use for computer audio` action.
6. Route PipeWire to one AirPlay-capable room.
7. Use Sonos grouping to include additional rooms in sync.
8. Fall back to Omarchy MPRIS metadata when AirPlay does not provide track metadata.

The plugin should not install packages, modify PipeWire configuration, or open firewall ports automatically. The current machine does not have `pipewire-zeroconf` installed. Sonos RAOP may require incoming UDP `6001-6002`, and the expected startup delay is roughly one to two seconds.

## Delivery Plan

1. Create the repository skeleton, manifest, dependency lock, README, and validation workflow.
2. Implement and unit-test discovery, normalization, cached hosts, target selection, and snapshot generation.
3. Implement playback, capability detection, progress, group volume, and room volume.
4. Implement serialized grouping reconciliation and failure verification.
5. Add `Service.qml`, process restart backoff, JSON parsing, command queueing, and IPC.
6. Build the theme-aware, keyboard-first `Widget.qml` views.
7. Add fake-backend fixtures for UI development without Sonos hardware.
8. Test against real Sonos hardware and external changes made from the Sonos app.
9. Publish the controller release.
10. Add optional PipeWire/AirPlay routing after the controller is stable.

## Verification

Automated checks:

- `omarchy plugin validate .`
- `shellcheck sonos-backend`
- Python formatting, linting, typing, and `pytest`.
- Protocol tests ensuring stdout contains valid JSON only.
- Fake SoCo tests for discovery, topology, metadata, coordinator changes, and partial failures.
- Fake backend tests for QML setup, offline, playing, grouped, and error states.

Hardware scenarios:

- One standalone room.
- Two standalone rooms grouped and ungrouped.
- Removing a follower.
- Removing the active coordinator.
- Moving a room between two playing groups.
- Stereo pairs and home-theater bonds.
- Spotify Connect, radio, TV, line-in, and AirPlay.
- External grouping and volume changes from the Sonos app.
- Speaker disconnect and recovery.
- Blocked callback ports with polling fallback.
- Multi-monitor operation confirming one backend process.
- Plugin hot reload confirming no orphan subprocesses.

Before implementation, the remaining useful inputs are the intended GitHub owner/plugin ID and the Sonos models available for hardware testing.

## Project Decisions

- Repository: `/home/ctl/Projects/omasonos`
- Plugin ID: `io.github.teevans.omasonos`
- License: MIT
- Primary target: Sonos S2
- Backend: pinned SoCo in an isolated XDG virtual environment
- Scope: full controller first, optional AirPlay/PipeWire phase afterward
