# Build plan review

> Historical implementation review. The README, manifest, and current source
> are authoritative for release behavior.

This repository starts from the uploaded `PLAN.md` and keeps its central
architecture: a combined Omarchy `service` + `bar-widget` plugin, one Python
backend process, a JSON-lines protocol, and SoCo/local UPnP rather than the
Sonos cloud API.

## Corrections made before implementation

### Manifest contract

The plan's manifest excerpt only showed `schemaVersion`, `kinds`, entry points,
and bar-widget metadata. Current Omarchy validation also requires `id`, `name`,
and `version`. The implementation therefore uses a complete manifest and sets
`keepLoaded: true` for the singleton service.

### Plugin namespace

The plan recorded `io.github.teevans.omasonos`. The GitHub account connected for
this build is `ctl0v0`, so the implementation uses:

```text
io.github.ctl0v0.omasonos
```

This keeps the reverse-DNS-style plugin identity aligned with the intended
repository owner.

### SoCo version

The plan pins SoCo `0.31.2`. A later verification against current PyPI metadata
confirmed that `0.31.2` was released on July 29, 2026 and includes Python 3.14
classification. Milestone one therefore pins `soco==0.31.2`.

## Architecture decisions retained

- One long-running backend process per Omarchy service, regardless of monitor count.
- Widget instances resolve shared state through `bar.shell.serviceFor(moduleName)`.
- Stdout is JSON protocol only; backend diagnostics go to stderr.
- XDG data/state locations are used; no `sudo`, Sonos account, or cloud token is required.
- A stable room UID anchors the selected group; a playing group is the fallback.
- Sonos households are explicit boundaries for grouping operations.
- Logical rooms come from visible zones, avoiding bonded hidden components in normal UI.
- Mutations are serialized through the single protocol loop and followed by a fresh snapshot.
- Group reconciliation adds first, removes followers next, removes the old coordinator last,
  verifies topology, and only stops the detached old coordinator after confirming it is standalone.
- `available_actions` drives seek/transport availability instead of interpreting command errors as capabilities.

## Milestone-one implementation

Implemented now:

- Cached-host + SSDP discovery.
- Household / logical room / group normalization.
- Remembered active-room anchor and playing-group fallback.
- Now-playing metadata, source, artwork, progress, duration, and available actions.
- Play/pause/previous/next/seek.
- Group volume/mute and per-room volume/mute.
- Explicit group selection across households.
- Staged room membership, Everywhere, Cancel, Apply, and cross-household rejection.
- Process bootstrap with isolated XDG virtual environment and setup lock.
- Process restart backoff in QML.
- Polling at 2 seconds while the card is open and 10 seconds while closed.
- Unit tests for state selection, persistence, JSON protocol behavior, and a critical
  cross-household grouping safety invariant.

## Intentionally deferred

The plan is larger than a safe first commit. These are deliberately next rather
than half-implemented in the controller core:

- Sonos event subscriptions and callback-port diagnostics/fallback behavior.
- Full keyboard navigation using `KeyboardPanel` / `PanelKeyCatcher`.
- More sophisticated large-household grouping layout and busy feedback.
- Fully transitive hash-locked Python dependency generation on the target Arch/Python 3.14 system.
- Rich fake-SoCo topology simulations for coordinator replacement and partial failures.
- Real Sonos S2 hardware validation across Spotify Connect, radio, TV, line-in, AirPlay,
  stereo/home-theater bonds, disconnect/recovery, and changes made in the Sonos app.
- Optional PipeWire/AirPlay system-audio routing phase.

The key sequencing decision is unchanged: make the local Sonos controller solid
before adding Linux system-audio routing.
