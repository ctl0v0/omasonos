# Changelog

All notable changes to OmaSonos are documented here.

## [0.2.1] - 2026-08-19

### Fixed

- Preserved command, setup, and degraded-network errors until users can act on them.
- Stopped retrying failed dependency setup in a background loop; retry is now explicit.
- Added keyboard operation and scrolling for long controller panels.
- Aggregated panel-open state across monitors for the shared backend service.
- Validated and installed from a clean staged plugin using Omarchy's official contract.

### Removed

- Removed internal build plans and implementation-review notes from the release tree.

## [0.2.0] - 2026-08-19

### Added

- Event-driven topology, transport, group-volume, and room-volume updates with
  an automatic polling fallback.
- Sonos Favorites playback for direct streams, queueable containers, and saved
  TuneIn podcasts.
- Playback handoff between standalone rooms and richer playback metadata.
- Discovery diagnostics, stale-state handling, and event subscription renewal.

### Changed

- Improved coordinator handoff verification and rollback behavior.
- Added reproducible, hash-locked runtime dependencies.
- Hardened backend restart handling and private state-file permissions.

## [0.1.1] - 2026-08-12

- Added local installation and validation scripts.
- Improved room activity display and audio handoff behavior.

## [0.1.0] - 2026-08-11

- Initial OmaSonos controller, service, and bar widget.
