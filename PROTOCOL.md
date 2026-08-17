# SEM6000 BLE protocol

Reference for the wire protocol, as implemented in
[`sem6000/protocol.py`](sem6000/protocol.py).

The reverse engineering behind this is
[Heckie75's](https://github.com/Heckie75/voltcraft-sem-6000/blob/master/API.md).
This document records what was **re-verified against real hardware** (a
SEM6000 CH, `VOLCFT`, firmware 1.15, hardware 2.0), plus the details that
turned out to matter in practice and the two places the published notes are
inconsistent.

## GATT

Service `0000fff0-0000-1000-8000-00805f9b34fb`:

| Characteristic | Properties | Use |
|---|---|---|
| `fff1` | read | vendor, firmware, hardware version |
| `fff2` | read | unused |
| `fff3` | write-without-response | commands go here |
| `fff4` | notify | responses arrive here |
| `fff5`, `fff6` | read | unused |

`fff1` is a plain read, not a framed message:

```
56 4f 4c 43 46 54 04 00 03 00 00 01 0f 02 00 19
|___________|                    |____| |____|
 "VOLCFT"                        fw 1.15  hw 2.0
```

## Frame format

```
0f <len> <payload...> <checksum> ff ff
```

| Field | Meaning |
|---|---|
| `0f` | start byte |
| `len` | count of bytes that follow it, **including** the checksum |
| `payload` | command byte, sub-command, arguments |
| `checksum` | `(sum(payload) + 1) & 0xff` |
| `ff ff` | terminator — **optional**, see below |

Multi-byte integers are big-endian.

The `+ 1` in the checksum is easy to miss and is what makes the device ignore
otherwise well-formed commands. Every documented example satisfies this rule,
and the test suite asserts that.

### The terminator is not guaranteed

The measurement response fills the notification, leaving no room for `ff ff`,
and simply omits it:

```
0f 11 04 00 01 00 5f 18 e7 00 df 32 00 00 00 00 00 00 75
                                                       ^^ checksum, then nothing
```

A parser that requires the terminator will hang on every measurement. Use the
length byte to delimit frames and treat the terminator as optional.

### Responses are fragmented

The device negotiates a 23-byte MTU on hardware < 3, capping notifications at
20 bytes. Anything longer arrives in pieces and must be reassembled by
accumulating until `2 + len` bytes are present. The serial response, at 25
bytes, always spans two notifications.

## Commands

| Byte | Command | Implemented |
|---|---|---|
| `0x01` | set date/time | yes |
| `0x02` | set device name | no |
| `0x03` | switch relay | yes |
| `0x04` | measure | yes |
| `0x05` | set overload cutoff | yes |
| `0x08` / `0x09` | set / read countdown timer | no |
| `0x0a` / `0x0b` / `0x0c` | history: hour / day / month | yes |
| `0x0f` | LED, prices, reset (sub-command in payload) | LED only |
| `0x10` | read settings | yes |
| `0x11` | read serial | yes |
| `0x17` | authorize, change PIN, reset PIN | yes |

Weekly scheduler and random mode also exist and are not implemented here.

### Authorize — `0x17`

Must be the first command after **every** connect; the session lasts only as
long as the BLE link.

```
0f 0c 17 00 00 <p0 p1 p2 p3> 00 00 00 00 <cks> ff ff
         |  |  |  |
         |  |  |  +- PIN, one byte per digit: "1234" -> 01 02 03 04
         |  |  +---- 0x00 = authorize, 0x01 = change PIN, 0x02 = reset PIN
         +--+------- command 0x1700
```

Response `0f 06 17 00 00 <status> 00 <cks> ff ff`, where status `0` = success.

In practice the device **ignores the first command on a fresh connection**
fairly often, so authentication should retry rather than fail.

### Switch — `0x03`

```
0f 06 03 00 <state> 00 00 <cks> ff ff      state: 01 = on, 00 = off
```

Response `0f 04 03 00 <status> <cks> ff ff`.

The relay changes immediately, but the reported **power lags about one
sampling window** — read power right after switching and you get the previous
load.

### Measure — `0x04`

Request `0f 05 04 00 00 00 05 ff ff`, response payload:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `0x04` |
| 1 | 1 | `0x00` |
| 2 | 1 | relay state, 1 = on |
| 3 | 3 | power, milliwatts |
| 6 | 1 | voltage, volts |
| 7 | 2 | current, milliamps |
| 9 | 1 | frequency, Hz |
| 10 | 4 | lifetime energy, Wh |

Worked example, captured from hardware:

```
04 00 01 00 5f 18 e7 00 df 32 00 00 00 00
      |  |________| |  |____| |  |________|
      on  24.344 W  231 V     |  0 Wh
                     0.223 A  50 Hz
```

**The lifetime energy counter is always 0 on hardware below 3.** The field is
present and never written. Check the hardware version from `fff1` before
trusting it; use the history commands or client-side integration instead.

Voltage is measured upstream of the relay and still reads ~230 V with the
socket off.

### History — `0x0a`, `0x0b`, `0x0c`

Request `0f 05 <kind> 00 00 00 <cks> ff ff`. Responses are arrays of Wh
values, oldest first, with the current bucket last:

| Kind | Buckets | Record | Payload |
|---|---|---|---|
| `0x0a` | 24 hours | 2 bytes | `24*2 + 2 + 1 = 0x33` |
| `0x0b` | 30 days | 4 bytes (3-byte value + pad) | `30*4 + 2 + 1 = 0x7b` |
| `0x0c` | 12 months | 4 bytes (3-byte value + pad) | `12*4 + 2 + 1 = 0x33` |

> **The device clock must be set or every bucket reads zero.**
>
> The plug files energy by its own real-time clock and does not keep time
> across a power cut. Until `0x01` has been sent, all three history queries
> return nothing but zeros — indistinguishable from an idle socket. This was
> confirmed here: history was uniformly zero, and the current hour began
> accumulating within minutes of setting the clock.

### Set date/time — `0x01`

```
0f 0c 01 00 <sec> <min> <hour> <day> <month> <year_hi> <year_lo> 00 00 <cks> ff ff
```

### Settings — `0x10`

Request `0f 05 10 00 00 00 11 ff ff`. Response payload:

| Offset | Field |
|---|---|
| 2 | reduced-tariff mode active |
| 3 | normal price ÷ 100 |
| 4 | reduced price ÷ 100 |
| 5–6 | reduced period start, minutes |
| 7–8 | reduced period end, minutes |
| 9 | LED ring, 1 = on |
| 11–12 | overload cutoff, watts |

The cutoff distinguishes regional variants: `0x0906` = 2310 W on the 10 A CH
unit, `0x0e60` = 3680 W on the 16 A EU unit.

## Errata in the published notes

Two documented examples carry checksums that do not match their own payloads —
both appear copied from the authorize example:

| Command | Published | Correct |
|---|---|---|
| change PIN `0f 0c 17 00 01 01 02 03 04 00 00 00 00 …` | `0x18` | `0x22` |
| reset PIN `0f 0c 17 00 02 00 00 00 00 00 00 00 00 …` | `0x18` | `0x1a` |

The `(sum + 1) & 0xff` rule, which every other example satisfies, is used here.

Note also that the measurement response's length byte is unreliable on
hardware ≥ 3 — the published sample declares `0x0f` while carrying 17 bytes.
Parse measurement fields by offset rather than trusting the declared length.

## Credit

Protocol reverse engineering by
[Heckie75](https://github.com/Heckie75/voltcraft-sem-6000). This document adds
hardware re-verification, the fragmentation and clock findings, and the errata
above.
