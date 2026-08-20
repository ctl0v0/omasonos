# OmaSonos

OmaSonos is a local-first Sonos controller for the Omarchy bar. It discovers
Sonos speakers on the local network and provides now-playing information,
transport controls, volume, Favorites, room handoff, and group management
without requiring Sonos cloud credentials.

> OmaSonos is beta software for Omarchy Quattro and Sonos S2. Core controls have
> been tested on a six-room household; broader hardware and network validation
> is still in progress.

![OmaSonos controller preview](preview.png)

## Features

- Now-playing metadata, artwork, seek, previous, play/pause, and next.
- Group volume and mute, plus per-room volume and mute controls.
- Explicit switching between independent playback sessions.
- Playback handoff between standalone rooms.
- Staged room grouping with Everywhere, Cancel, and Apply actions.
- Sonos Favorites for direct radio/audio streams, queueable albums and
  playlists, and the newest episode of saved TuneIn podcasts.
- Event-driven updates with an automatic polling fallback when Sonos cannot
  reach the local callback listener.
- Cached speaker discovery and remembered room selection across restarts.

## Architecture

```text
Widget.qml (one per bar/monitor)
    |
Service.qml (one shared Omarchy service)
    |
JSON Lines over stdin/stdout
    |
sonos_service.py + omasonos_backend/
    |
SoCo / local UPnP
    |
Sonos household
```

`Service.qml` owns the backend process and exposes its latest authoritative
snapshot to every widget instance. The Python protocol boundary serializes all
playback and topology mutations, then emits a fresh snapshot after each command.
The plugin ID is `io.github.ctl0v0.omasonos`.

## Requirements

- Omarchy Quattro with the current shell plugin commands.
- Sonos S2 speakers reachable from the same local network.
- Python 3.14 with `venv`, Bash, `flock`, `sha256sum`, and Internet access on the
  first backend start to install the hash-locked Python dependencies.
- A network policy that permits HTTP/UPnP access to the speakers and, for live
  events, speaker callbacks to this machine on TCP ports `1400-1499`.

The direct runtime dependencies are [SoCo 0.31.2](https://github.com/SoCo/SoCo)
and [Requests 2.34.2](https://requests.readthedocs.io/). All transitive Python
dependencies and artifact hashes are recorded in `requirements.lock`.

## Install

```bash
omarchy plugin add https://github.com/ctl0v0/omasonos.git --enable
```

The first backend start creates an isolated virtual environment at:

```text
${XDG_DATA_HOME:-~/.local/share}/io.github.ctl0v0.omasonos/venv
```

The selected room and cached speaker addresses are stored with owner-only
permissions at:

```text
${XDG_STATE_HOME:-~/.local/state}/io.github.ctl0v0.omasonos/state.json
```

No Sonos account credentials or cloud tokens are requested or stored.

## Update

```bash
omarchy plugin update io.github.ctl0v0.omasonos --yes
```

## Remove

Disable and remove the plugin through Omarchy:

```bash
omarchy plugin disable io.github.ctl0v0.omasonos
omarchy plugin remove io.github.ctl0v0.omasonos --yes
```

Omarchy removes the plugin checkout but leaves its cached virtual environment
and state so a reinstall can reuse them. To erase those files too:

```bash
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/io.github.ctl0v0.omasonos"
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/io.github.ctl0v0.omasonos"
```

These paths contain only OmaSonos data. Removing them does not change speaker
configuration or delete Sonos Favorites.

## Controls

- Left click opens or closes the controller card.
- Middle click toggles play/pause.
- The mouse wheel changes group volume in 5% steps.
- Arrow keys or `j`/`k` move between actions; `h`/`l` adjust group volume.
- `Space` activates the focused action, `n`/`p` change tracks, and `m` toggles mute.
- `g` toggles playback sessions, `r` toggles group settings, and `Escape` closes.
- `Playing on` moves a standalone session to another standalone room.
- `Control different audio` changes which independent Sonos session is targeted.
- `Favorites` starts a compatible saved Sonos Favorite.
- `Group settings` stages and applies room membership changes.

Transport buttons honor the actions reported by Sonos. Seek is shown only for a
source that reports `SeekTime` and a duration.

## Network and privacy

Normal discovery tries cached speaker addresses, then a five-second SSDP window.
If both fail, OmaSonos rate-limits a scan of attached private IPv4 networks to
once per minute while offline. When callbacks are reachable, SoCo listens on TCP
ports `1400-1499` for local Sonos topology, transport, and volume events.

Most control remains on the LAN. Starting a saved TuneIn podcast contacts TuneIn
and its media host, and remote artwork URLs may be loaded by the widget. See
[SECURITY.md](SECURITY.md) for the complete capability and storage summary.

## Local development

Install or update a checkout on an Omarchy machine:

```bash
./scripts/install-local.sh
./scripts/test-local.sh
```

The test wrapper validates a clean staged plugin with Omarchy's authoritative
validator, lints QML when `qmllint` is available, checks Omarchy registration,
runs unit tests when `pytest` is available, and performs a backend discovery smoke test.
The smoke test can create the runtime virtual environment, discover and probe
speakers, open the SoCo callback listener, and update cached discovery state. It
does not issue playback, volume, or grouping commands.

To probe a known speaker explicitly:

```bash
./scripts/smoke-backend.py --host 192.168.1.42
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development environment,
validation commands, and dependency update process.

## Known limitations

- The first launch requires access to the configured Python package index.
- Discovery is optimized for one household; additional Sonos households may be
  found through cached hosts or the attached-network fallback but are not yet a
  guaranteed discovery path.
- Sonos S1 hardware is not currently supported or tested.

## License

OmaSonos is available under the [MIT License](LICENSE). Sonos is a trademark of
Sonos, Inc. This project is not affiliated with or endorsed by Sonos or Omarchy.
