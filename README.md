# OmaSonos

A local-first Sonos controller for Omarchy. OmaSonos runs one headless service
inside `omarchy-shell`, talks JSON-lines to a small Python process, and uses
SoCo/local UPnP rather than the Sonos cloud API.

> Status: early controller milestone. Discovery, normalized state, remembered
> target room, playback controls, group/room volume, and grouping reconciliation
> exist in the backend. The bar widget exposes now-playing, seek (when supported),
> transport, group switching, group volume, a per-room mixer, and staged room
> grouping. Event subscriptions, full keyboard navigation, and hardware validation
> are the next milestones.

## Architecture

```text
Widget.qml
    |
Service.qml                  one instance per Omarchy shell
    |
JSON-lines subprocess
    |
sonos_service.py
    |
SoCo / local UPnP
    |
Sonos household
```

The plugin ID is `io.github.ctl0v0.omasonos`.

## Install on Omarchy

Once this repository exists on GitHub:

```bash
omarchy plugin add https://github.com/ctl0v0/omasonos.git
omarchy plugin enable io.github.ctl0v0.omasonos
```

The first backend start creates an isolated virtual environment at:

```text
${XDG_DATA_HOME:-~/.local/share}/io.github.ctl0v0.omasonos/venv
```

Persistent selection and discovered speaker addresses live at:

```text
${XDG_STATE_HOME:-~/.local/state}/io.github.ctl0v0.omasonos/state.json
```

No Sonos account credentials or cloud tokens are stored.

## Current controls

- Left click: open/close the controller card.
- Middle click: play/pause.
- Wheel: group volume ±5%.
- Previous / play-pause / next honor Sonos `available_actions`.
- Group volume and mute are wired to the backend.
- Multiple active groups can be selected explicitly.
- The room mixer exposes per-room volume and mute.
- Room membership is staged locally with Everywhere / Cancel / Apply.
- Seek appears only when Sonos reports `SeekTime` support and a duration.

## Development

On any Python with `pytest` available:

```bash
python -m pytest -q
python -m compileall -q sonos_service.py omasonos_backend
bash -n sonos-backend
python tests/validate_manifest.py
```

On Omarchy, also run the authoritative validator:

```bash
omarchy plugin validate .
```

You can exercise the backend protocol without QML (after installing SoCo):

```bash
python -u sonos_service.py
```

Then send one JSON object per line, for example:

```json
{"id":"1","op":"refresh"}
{"id":"2","op":"playPause"}
{"id":"3","op":"setGroupVolume","volume":35}
```

Stdout is reserved for JSON protocol messages; diagnostics go to stderr.

## Dependency note

The implementation pins SoCo `0.31.1`. The uploaded planning document named
`0.31.2`, but that release does not exist. Before a public/controller release,
`requirements.lock` still needs to be regenerated as a fully transitive,
hash-locked Python 3.14 lockfile on the target Arch environment.

## Next milestones

1. Add the full keyboard model and polish the group/room views for large households.
2. Add event subscriptions for topology, AVTransport, group volume, and rendering control with polling fallback.
3. Add richer fake-SoCo fixtures for coordinator removal, cross-group moves, partial topology failures, and multiple households.
4. Validate on real Sonos S2 hardware and against external changes made in the Sonos app.
5. Add the optional PipeWire/AirPlay phase only after controller stability.
