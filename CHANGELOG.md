# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Lowered the `bleak` floor from `>=0.21` to `>=0.20`, so Debian 12 and
  Raspberry Pi OS Bookworm can use the distro `python3-bleak` (0.20.2) instead
  of building anything. The library only needs APIs present since 0.20; the
  full suite and a live device session were verified on 0.20.2 and on 3.x.
- `--address`, `--pin` and `--json` are accepted before *or* after the
  subcommand. Previously only the global position worked, while the
  missing-address error told the user to pass `--address` — which then failed.
  Subcommand `-h` now lists them too.

### Added

- Raspberry Pi installation and deployment notes.
- systemd units for the logger and the exporter in [`deploy/`](deploy/).
- `tests/test_cli.py`: every subcommand against every global option in both
  positions.

### Documented

- The plug accepts a **single BLE connection**, and a second client takes the
  link over rather than being refused. The logger recovers from this on its
  own; `auto_disconnect_stale=False` opts out of taking over.

## [0.1.0] - 2026-08-17

First release. Developed and verified against a SEM6000 CH (`VOLCFT`,
firmware 1.15, hardware 2.0) on 230 V / 50 Hz mains.

### Added

- Async client (`SEM6000`) over [bleak](https://github.com/hbldh/bleak) —
  no `bluepy`, no compiled helper.
- Blocking facade (`SEM6000Sync`) with a `measuring()` context manager that
  reports the energy drawn by a block of synchronous code.
- Switching (`turn_on`, `turn_off`, `toggle`) and instantaneous readings
  (power, voltage, current, frequency, apparent power, power factor).
- Stored energy history at hourly, daily and monthly resolution.
- Client-side trapezoidal energy integration (`sem6000.energy`).
- Recording to CSV and SQLite with reconnect-and-backoff (`sem6000.logger`).
- Prometheus exporter using only the standard library (`sem6000.prometheus`).
- Device clock sync, LED control, overload cutoff, PIN change and reset.
- `sem6000` command line covering all of the above.
- Protocol reference in [PROTOCOL.md](PROTOCOL.md); 67 hardware-free tests.

### Notes on device behaviour

Discovered during development and handled by the library:

- The stored history stays at zero until the **device clock is set**, and the
  clock does not survive a power cut. `sync_time()` is required to make any
  energy history work.
- The **lifetime energy counter is always 0 on hardware below 3**; the field
  exists but is never populated. `Measurement.energy_available` reports this.
- A **connected plug does not advertise**, so address lookup fails with
  "device not found". The client drops the stale BlueZ link and retries.
- Responses are **fragmented** across a 20-byte notification limit, and the
  `ff ff` terminator is **omitted** from the measurement response.
- The device intermittently **ignores the first command** after connecting;
  authentication retries.

### Errata

Two examples in the published protocol notes carry checksums that do not match
their own payloads (change PIN, reset PIN). See
[PROTOCOL.md](PROTOCOL.md#errata-in-the-published-notes).

[0.1.0]: https://github.com/trimm-b/sem6000/releases/tag/v0.1.0
