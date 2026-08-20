# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. If that is
unavailable, open a public issue containing only a request for private contact;
do not include exploit details, speaker addresses, household identifiers, or
other sensitive information.

Include the OmaSonos version, Omarchy version, Sonos firmware generation, and a
minimal reproduction when it is safe to do so.

## Security model

Omarchy plugins run unsandboxed as the current user. OmaSonos starts a local
Python subprocess and communicates with Sonos speakers over the local network.
It does not require `sudo`, Sonos credentials, cloud tokens, or an inbound
Internet connection.

The plugin does perform the following network and storage operations:

- SSDP discovery and direct HTTP/UPnP requests to Sonos speakers.
- A rate-limited scan of attached private IPv4 networks when normal discovery
  and cached addresses both fail.
- A local SoCo callback listener on TCP ports `1400-1499` for Sonos events.
- HTTPS requests to TuneIn and podcast media hosts when starting a saved TuneIn
  podcast, plus artwork requests made by the QML image component.
- First-run installation of hash-locked Python packages from the configured pip
  package index into a plugin-specific virtual environment.
- Storage of the selected room UID and cached speaker IP addresses in an
  owner-only state directory and file. No playback history is persisted.

Review `sonos-backend`, `requirements.lock`, and the Python backend before
installing if these capabilities do not fit your threat model.
