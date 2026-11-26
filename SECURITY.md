# Security policy

RingKeeper handles sensitive health data (including reproductive/cycle data), Bluetooth
authentication keys for a wearable, and bearer tokens for its ingest service. We take
reports seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository
(<https://github.com/Bluecollarlabs/ringkeeper/security/advisories/new>).

Include what you found, how to reproduce it, and the impact. Do **not** include anyone's real
health data, keys or tokens. We'll acknowledge within 7 days and aim to fix critical issues
within 30 days. We're happy to credit you.

## Scope

In scope: the ingest service, dashboard API (`tools/dash_api.py`), export/delete
(`tools/privacy.py`), the Android app and Rust core, scripts in this repo, and anything that
could leak data, keys or tokens or let someone else pair with or read a user's ring.

Out of scope: vulnerabilities in Oura's own products (report those to Oura Health Oy),
upstream open_oura (report upstream, and tell us if it affects RingKeeper), Tailscale,
Ollama, BlueZ or Android themselves.

## Design assumptions

- Services bind to loopback only and are published only on a private tailnet with
  `tailscale serve`. Exposing them to the internet (`tailscale funnel`, port forwarding) is
  unsupported.
- Token files, keys and data are created with mode 600 / 700; the ingest service refuses a
  world-readable `tokens.json`.
- The local AI client refuses non-loopback URLs.

## Supported versions

Only the latest `main` is supported while the project is pre-1.0.
