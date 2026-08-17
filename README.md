# sem6000

A modern Python API for the **Voltcraft SEM6000** Bluetooth LE smart plug —
switch it, read live power, and track energy over time.

Built on [bleak](https://github.com/hbldh/bleak): pure Python, cross-platform,
no compiled helper binary, no root.

```python
async with SEM6000("AA:BB:CC:DD:EE:FF") as plug:
    m = await plug.measure()
    print(f"{m.power_w:.1f} W")     # 26.4 W
    await plug.turn_off()
```

Verified end to end against a **SEM6000 CH** (`VOLCFT`, firmware 1.15,
hardware 2.0) on 230 V / 50 Hz mains.

---

## Contents

- [Why this exists](#why-this-exists)
- [Getting started](#getting-started)
- [Python API](#python-api)
- [Blocking API](#blocking-api)
- [Energy over time](#energy-over-time)
- [Command line](#command-line)
- [Monitoring](#monitoring)
- [Safety and security](#safety-and-security)
- [Notes from the hardware](#notes-from-the-hardware)
- [Development](#development)
- [Credit and inspiration](#credit-and-inspiration)

---

## Why this exists

The SEM6000 protocol was reverse engineered years ago and documented well by
[Heckie75](https://github.com/Heckie75/voltcraft-sem-6000). What was missing was
a Python library you could just install:

- Nothing on PyPI. `sem6000`, `voltcraft-sem6000` and
  `python3-voltcraft-sem6000` are all unclaimed.
- The existing Python implementations are forks of a single codebase and all
  depend on [`bluepy`](https://github.com/IanHarvey/bluepy), whose last release
  was **December 2018** and which needs a compiled helper binary with elevated
  capabilities.
- The most complete implementation of all is a shell script driving `expect`.

This library reuses the protocol knowledge wholesale — it is not a fresh
reverse engineering effort — and puts a tested, async, dependency-light Python
API on top.

## Getting started

### 1. Install

```bash
git clone https://github.com/trimm-b/sem6000
cd sem6000
pip install -e .
```

Requires Python 3.9+ and a Bluetooth LE adapter. On Linux you also need BlueZ,
which is almost certainly already installed.

### 2. Pair the plug

Once, using your system's Bluetooth tooling. On Linux:

```bash
bluetoothctl
[bluetooth]# scan on          # look for a device named "Voltcraft"
[bluetooth]# pair 78:DB:2F:...
[bluetooth]# trust 78:DB:2F:...
[bluetooth]# quit
```

### 3. Find its address

```bash
$ sem6000 discover
AA:BB:CC:DD:EE:FF  Voltcraft        rssi -54
```

> A plug that is already **connected** will not show up — BLE peripherals stop
> advertising once connected. Disconnect it first, or just use the address you
> paired with.

### 4. Say hello

```bash
export SEM6000_ADDRESS=AA:BB:CC:DD:EE:FF

$ sem6000 status
device    VOLCFT fw 1.15 hw 2.0
socket    ON
power       26.435 W
voltage        230 V
current      0.235 A
apparent     54.05 VA
pf            0.49
frequency       50 Hz
```

The default PIN is `0000`. If yours differs, pass `--pin` or set
`SEM6000_PIN`.

### 5. Set the clock — do this once

```bash
sem6000 sync-time
```

**Skip this and all energy history stays at zero forever.** The plug files
energy into hourly/daily/monthly buckets using its own real-time clock, and it
does not keep time across a power cut. See
[Energy over time](#energy-over-time).

### 6. Switch something

```bash
sem6000 off
sem6000 on
```

## Python API

```python
import asyncio
from sem6000 import SEM6000

async def main():
    async with SEM6000("AA:BB:CC:DD:EE:FF", pin="0000") as plug:
        print(plug.info)                # DeviceInfo(vendor='VOLCFT', ...)

        m = await plug.measure()
        print(m.on, m.power_w, m.voltage_v, m.current_a)

        await plug.turn_on()
        await plug.turn_off()
        await plug.toggle()

        print(await plug.settings())
        print(await plug.serial())

asyncio.run(main())
```

The context manager connects **and authenticates** on entry — the PIN must be
sent after every connect, since the session lives only as long as the BLE link.

### What a measurement gives you

| Attribute | Meaning |
|---|---|
| `on` | relay state |
| `power_w` | real power, W |
| `voltage_v` | mains voltage, V |
| `current_a` | current, A |
| `frequency_hz` | mains frequency, Hz |
| `apparent_va` | `voltage x current` |
| `power_factor` | real ÷ apparent, `None` when nothing is drawing |
| `energy_wh` | lifetime counter — see the caveat below |
| `energy_available` | whether `energy_wh` means anything on your hardware |

Voltage is measured upstream of the relay, so it still reads ~230 V while the
socket is off.

## Blocking API

For scripts, notebooks and the REPL, where an event loop is more ceremony than
it is worth:

```python
from sem6000 import SEM6000Sync

with SEM6000Sync("AA:BB:CC:DD:EE:FF") as plug:
    print(plug.measure().power_w)
    plug.turn_off()
```

It can also measure a block of perfectly ordinary synchronous code:

```python
with plug.measuring() as energy:
    train_the_model()

print(f"that cost {energy.energy_wh:.3f} Wh, {energy.cost(0.32):.4f} at 0.32/kWh")
```

A private event loop runs on a background thread for the object's lifetime, so
one BLE connection persists across calls.

## Energy over time

This is the part with a hardware caveat worth understanding.

The measurement frame carries a lifetime energy counter — but **hardware
revisions below 3 leave it permanently at zero**. The field exists and is never
written. `m.energy_available` tells you which side of that line you are on.

So there are three ways to get energy, and on hardware 2 you want the last two.

### 1. The device's stored history

```python
await plug.history_hourly()     # 24 values in Wh, oldest first
await plug.history_daily()      # 30 values
await plug.history_monthly()    # 12 values
await plug.energy_today_wh()
```

```bash
$ sem6000 history hour
   - 3 hour       0 Wh
   - 2 hour       0 Wh
   - 1 hour       0 Wh
       now      10 Wh  ################################
     total      10 Wh
```

(The zeros above are real: this plug's clock had just been set, so it had
nothing older to report.)

> ### The plug must know the time
>
> Until the clock is set, **every history query returns zeros** — which is
> indistinguishable from an idle socket, and is the single least obvious thing
> about this device. Run `sem6000 sync-time` (or `await plug.sync_time()`)
> once, and again after any power cut.

### 2. Integrate live readings

For anything shorter than an hour, sample and integrate. The trapezoidal rule
handles the ramps at the edges of a load properly:

```python
from sem6000.energy import measure_energy

it, _ = await measure_energy(plug, duration_s=60, interval_s=1.0)
print(it.energy_wh, it.mean_w, it.peak_w, it.cost(0.32))
```

The device refreshes its own figures about once a second, so polling faster
buys no accuracy.

### 3. Log to disk

For tracking across days:

```bash
sem6000 log --sqlite power.db --csv power.csv --interval 5
```

Each row records the energy accumulated since the previous row, so summing over
any range is correct even when sampling was uneven:

```sql
SELECT SUM(energy_wh) FROM measurements WHERE timestamp >= '2026-08-17';
```

The logger reconnects with exponential backoff — BLE links drop routinely over
hours — and never integrates across a gap it did not actually measure.

### Worked example

[`examples/benchmark.py`](examples/benchmark.py) records an idle baseline, runs
a workload, and reports the difference. On the reference machine, 22 cores busy:

```
idle              26.35 W
under load        65.19 W
workload cost     38.84 W above idle
total energy       0.540 Wh over 30s
extrapolated       0.932 kWh/day if sustained
```

## Command line

```bash
sem6000 discover                    # scan for plugs
sem6000 status                      # current reading
sem6000 on | off | toggle           # switch the socket
sem6000 watch                       # live stream
sem6000 energy 60 --price 0.32      # integrate over 60 s, report cost
sem6000 history hour | day | month  # stored Wh, with a bar chart
sem6000 settings                    # LED, cutoff, tariffs, serial
sem6000 sync-time                   # set the device clock
sem6000 led on | off                # LED ring
sem6000 log --sqlite power.db       # record to disk
sem6000 export --port 9110          # Prometheus metrics
sem6000 set-limit 2300              # overload cutoff
sem6000 set-pin 4271                # change from the 0000 default
sem6000 reset-pin                   # back to 0000
```

`--json` works on `status`, `energy` and `history`, in either position.
`--address` / `--pin` override `SEM6000_ADDRESS` / `SEM6000_PIN`.

## Monitoring

```bash
sem6000 export --port 9110      # scrape http://localhost:9110/metrics
```

Serves `sem6000_power_watts`, `sem6000_energy_watthours_total`,
`sem6000_socket_on`, `sem6000_power_factor`, `sem6000_up` and friends in the
Prometheus text format, using only the standard library.

The plug is polled on a timer rather than on scrape, so a slow BLE round-trip
can never stall Prometheus, and many scrapers share one connection. `sem6000_up`
goes to 0 when polling fails, while the last good reading stays visible.

## Safety and security

```bash
sem6000 set-pin 4271       # change the PIN
sem6000 set-limit 2300     # overload cutoff, W
```

**The factory PIN is `0000`**, so out of the box anyone within Bluetooth range
can switch your socket. Changing it takes thirty seconds. Write the new one
down — recovering a forgotten PIN needs a physical reset of the plug.

Do not set the overload cutoff above the socket's rating: 2300 W for the 10 A
CH version, 3680 W for the 16 A EU version.

## Notes from the hardware

Things that cost time to work out, recorded so they need not be again:

- **A connected plug is invisible to a scan.** BLE peripherals stop advertising
  once connected, so an address lookup raises "device not found" even though the
  plug is right there. This library detects that and drops the stale BlueZ link
  before retrying.
- **Login is per-connection**, and the device fairly often ignores the very
  first command on a fresh link — so authentication retries by design.
- **Responses are fragmented.** A 23-byte MTU caps notifications at 20 bytes,
  so longer replies arrive in pieces and are reassembled.
- **The frame terminator is optional.** Most frames end `ff ff`, but the
  measurement response drops it — there is no room. A parser that waits for it
  hangs on every reading.
- **The relay leads the meter.** After switching, relay state is correct
  immediately but power lags about a second. Use `switch(on, settle=True)` when
  you are about to read power.
- **The checksum has a `+ 1`** in it, which is easy to miss and makes the
  device silently ignore otherwise well-formed commands.

Full details in [PROTOCOL.md](PROTOCOL.md), including two errata in the
published protocol notes.

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q          # 67 tests, no hardware required
```

[`sem6000/protocol.py`](sem6000/protocol.py) is pure functions — framing,
checksums, parsing, no I/O — so the whole protocol is testable without a plug.
The suite pins it against byte vectors captured from real hardware and against
every published example.

```
sem6000/
  protocol.py     framing, checksums, parsers   (no I/O)
  client.py       async BLE client
  sync.py         blocking facade
  energy.py       trapezoidal integration
  logger.py       CSV / SQLite recording
  prometheus.py   metrics exporter
  cli.py          command line
```

## Credit and inspiration

This library stands on other people's reverse engineering.

- **[Heckie75/voltcraft-sem-6000](https://github.com/Heckie75/voltcraft-sem-6000)**
  — the protocol documentation this is built on, and the original reverse
  engineering effort. Indispensable. MIT.
- **[moormaster/python3-voltcraft-sem6000](https://github.com/moormaster/python3-voltcraft-sem6000)**
  — the prior Python implementation (`bluepy`-based), and the ancestor of the
  various forks. MIT.
- **[ldb/spb012ble](https://codeberg.org/ldb/spb012ble)** — independent
  documentation of the SEM6000 SE hardware and firmware.
- **[bleak](https://github.com/hbldh/bleak)** — the cross-platform BLE stack
  that makes the `bluepy` dependency unnecessary.

Contributions of captured frames from other hardware revisions — especially
hardware ≥ 3, where the lifetime energy counter actually works — are welcome.

## License

MIT — see [LICENSE](LICENSE).
