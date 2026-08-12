# Development notes

## Milestone 1 contract

The QML layer never talks to Sonos directly. `Service.qml` owns exactly one
backend process and exposes the latest authoritative snapshot. `Widget.qml`
may be instantiated on more than one monitor, but all instances resolve the
same service via `bar.shell.serviceFor(moduleName)`.

Every state-changing JSON-lines command emits a `result` followed by a new
`snapshot`. The backend is single-threaded at the protocol boundary, which
serializes topology and playback mutations.

## Snapshot v1

Top-level keys:

- `status`: backend/discovery state.
- `households[]`: logical rooms and groups, isolated by Sonos household ID.
- `selectedAnchorRoomUid`: persisted stable room identity.
- `target`: the currently controlled group.
- `playback`: target-coordinator transport metadata and capabilities.

The frontend must treat the snapshot as authoritative after mutations.

## Not yet release-complete

- Topology, transport, group-volume, and room-rendering event subscriptions are
  implemented with a 75 ms event-burst window. Incomplete subscriptions fall
  back to polling; healthy event mode retains slower 10/30-second safety polls.
- The QML group picker, room mixer, staged grouping view, and seek UI are in
  place, but the full keyboard model and large-household layout still need work.
- `requirements.lock` is exact-pinned but not yet transitive/hash-locked.
- Discovery, callback reachability, and basic controller operations work on the
  six-room Sonos S2 household; the full hardware scenario matrix remains.
