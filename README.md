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


### Local checkout install

From a checkout on your Omarchy machine, you can install/update the development
copy without publishing to GitHub:

```bash
./scripts/install-local.sh
```

Then run the local test wrapper:

```bash
./scripts/test-local.sh
```

It validates the manifest, confirms `omarchy-shell` can see the plugin, runs the
Python tests when `pytest` is installed, and performs a read-only backend
discovery smoke test. To run only the backend check:

```bash
./scripts/smoke-backend.py
```

To explicitly test whether this machine can open Sonos's HTTP/UPnP port on a
known speaker, pass its address (repeat `--host` for multiple speakers):

```bash
./scripts/smoke-backend.py --host 192.168.1.42
```

The smoke test is successful even when no speakers are found; in that case it
prints `status: offline`. It also reports whether discovery found speakers via
the cached-host path, SSDP multicast, or the attached-network scan fallback.
On the real Sonos LAN it should also report households, rooms, and the selected
target group.
The report includes every local IPv4 interface/subnet, both discovery cycles,
and a TCP/1400 probe for each supplied or discovered speaker address.

Discovery tries cached speaker addresses first. When one responds, its Sonos
topology supplies the household rooms without adding an SSDP delay to routine
polls or commands. If all cached addresses miss, OmaSonos uses SoCo's normal
five-second SSDP window and then its local IPv4 network scanner (port 1400),
rate-limiting that heavier fallback to once per minute while offline.

When the machine is reachable from Sonos, OmaSonos subscribes to topology,
transport, group-volume, and room-volume events on TCP `1400-1499`. Event bursts
are coalesced for 75 ms before a fast refresh. If any required subscription or
callback listener fails, the service automatically retains its polling fallback.

## Current controls

- Left click: open/close the controller card.
- Middle click: play/pause.
- Wheel: group volume ±5%.
- Previous / play-pause / next honor Sonos `available_actions`.
- Group volume and mute are wired to the backend.
- Multiple active groups can be selected explicitly.
- `Playing on: …` shows the current destination and expands into room and
  actual multi-room-group choices for moving the current queue. Standalone
  rooms are not mislabeled as groups.
- `Favorites` caches directly playable Sonos Favorites and starts one on the
  current destination. Saved containers without a playable URI are reported
  but intentionally omitted.
- `Control different audio` is a separate collapsed action for switching which
  independent playback session the transport controls address without moving it.
- The room mixer exposes per-room volume and mute, with an accent-colored,
  pulsing music-note indicator beside every room whose Sonos group is active.
- Lower-priority room membership editing is collapsed under `Group settings`,
  with staged Everywhere / Cancel / Apply actions.
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

The implementation pins SoCo `0.31.2`, matching the planning document. Before a
public/controller release, `requirements.lock` still needs to be regenerated as a
fully transitive, hash-locked Python 3.14 lockfile on the target Arch environment.

## Next milestones

1. Add the full keyboard model and polish the group/room views for large households.
2. Add richer fake-SoCo fixtures for coordinator removal, cross-group moves, partial topology failures, and multiple households.
3. Validate the remaining real Sonos S2 scenarios and external changes made in the Sonos app.
4. Add the optional PipeWire/AirPlay phase only after controller stability.
