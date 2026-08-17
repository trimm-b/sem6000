"""Frame encoding/decoding for the Voltcraft SEM6000 BLE protocol.

Pure functions only - no I/O, no Bluetooth. Everything here is unit-testable
against the captured byte vectors in tests/test_protocol.py.

Wire format
-----------
    0f <len> <payload...> <checksum> ff ff

``len``      number of bytes that follow it, including the checksum byte.
``checksum`` ``(sum(payload) + 1) & 0xff``
``ff ff``    static terminator, *omitted* when the frame would otherwise
             overflow the 20-byte notification MTU (the measurement response
             does this).

Multi-byte numbers are big-endian.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

START = 0x0F
END = b"\xff\xff"

# GATT
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CHAR_INFO = "0000fff1-0000-1000-8000-00805f9b34fb"  # vendor / fw / hw, read
CHAR_WRITE = "0000fff3-0000-1000-8000-00805f9b34fb"  # commands, write-no-response
CHAR_NOTIFY = "0000fff4-0000-1000-8000-00805f9b34fb"  # responses, notify

# Commands
CMD_SET_DATETIME = 0x01
CMD_SET_NAME = 0x02
CMD_SWITCH = 0x03
CMD_MEASURE = 0x04
CMD_SET_OVERLOAD = 0x05
CMD_HISTORY_HOURLY = 0x0A  # last 24 h, per hour
CMD_HISTORY_DAILY = 0x0B  # last 30 days, per day
CMD_HISTORY_MONTHLY = 0x0C  # last 12 months, per month
CMD_MISC = 0x0F  # LED / prices / reset, sub-command in payload
CMD_SETTINGS = 0x10
CMD_SERIAL = 0x11
CMD_AUTH = 0x17


class ProtocolError(Exception):
    """Malformed or unexpected data on the wire."""


def checksum(payload: bytes) -> int:
    return (sum(payload) + 1) & 0xFF


def encode(payload: bytes) -> bytes:
    """Wrap a command payload in a complete frame."""
    body = payload + bytes([checksum(payload)])
    return bytes([START, len(body)]) + body + END


def decode(frame: bytes, *, verify: bool = True) -> bytes:
    """Unwrap a frame and return its payload (checksum and terminator stripped).

    Raises ProtocolError on a bad start byte, truncation, or checksum mismatch.
    """
    if len(frame) < 4:
        raise ProtocolError(f"frame too short: {frame.hex(' ')}")
    if frame[0] != START:
        raise ProtocolError(f"bad start byte {frame[0]:#04x}: {frame.hex(' ')}")

    length = frame[1]
    body = frame[2 : 2 + length]
    if len(body) < length:
        raise ProtocolError(
            f"truncated: header declares {length} bytes, got {len(body)}"
        )

    payload, got = body[:-1], body[-1]
    if verify:
        want = checksum(payload)
        if got != want:
            raise ProtocolError(
                f"checksum {got:#04x} != expected {want:#04x}: {frame.hex(' ')}"
            )
    return payload


def frame_length(header: bytes) -> Optional[int]:
    """Total on-wire size of the frame starting at ``header``, or None if the
    two header bytes are not yet available.

    Used by the reassembler: with a 23-byte MTU the device splits long
    responses across several 20-byte notifications, so we must know when a
    message is complete. The trailing ``ff ff`` is optional, so this returns
    the length *without* it; the reassembler treats the terminator as a
    bonus rather than a requirement.
    """
    if len(header) < 2:
        return None
    return 2 + header[1]


class Reassembler:
    """Accumulates notification chunks into complete frames.

    The SEM6000 negotiates a 23-byte MTU on hardware < 3, capping notifications
    at 20 bytes, so responses like the serial number arrive in pieces.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> List[bytes]:
        """Add a notification chunk; return any frames it completed.

        Emitted frames never include the trailing ``ff ff``. The terminator is
        optional on the wire and carries no information, so normalising it away
        keeps output uniform - and means we never have to block a finished
        frame waiting to find out whether a terminator is coming.
        """
        self._buf.extend(chunk)
        out: List[bytes] = []

        while True:
            if len(self._buf) < 2:
                break
            if self._buf[0] != START:
                # Resynchronise: drop bytes until a plausible start appears.
                idx = self._buf.find(START, 1)
                if idx < 0:
                    self._buf.clear()
                    break
                del self._buf[:idx]
                continue

            need = frame_length(bytes(self._buf[:2]))
            if need is None or len(self._buf) < need:
                break

            frame = bytes(self._buf[:need])
            del self._buf[:need]
            # Swallow the optional terminator when it is already here; when it
            # arrives later the resync step at the top of the loop drops it.
            if self._buf[:2] == END:
                del self._buf[:2]
            out.append(frame)

        return out

    def reset(self) -> None:
        self._buf.clear()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_authorize(pin: str) -> bytes:
    """Log in. Must be the first command after every connect."""
    digits = _pin_bytes(pin)
    return encode(bytes([CMD_AUTH, 0x00, 0x00]) + digits + bytes(4))


def cmd_switch(on: bool) -> bytes:
    return encode(bytes([CMD_SWITCH, 0x00, 1 if on else 0, 0x00, 0x00]))


def cmd_measure() -> bytes:
    return encode(bytes([CMD_MEASURE, 0x00, 0x00, 0x00]))


def cmd_settings() -> bytes:
    return encode(bytes([CMD_SETTINGS, 0x00, 0x00, 0x00]))


def cmd_serial() -> bytes:
    return encode(bytes([CMD_SERIAL, 0x00, 0x00, 0x00]))


def cmd_history(kind: int) -> bytes:
    if kind not in (CMD_HISTORY_HOURLY, CMD_HISTORY_DAILY, CMD_HISTORY_MONTHLY):
        raise ValueError(f"unknown history kind {kind:#04x}")
    return encode(bytes([kind, 0x00, 0x00, 0x00]))


def cmd_set_datetime(dt) -> bytes:
    """Set the device clock.

    The plug buckets its stored history by its own real-time clock, so an
    unset clock means the hourly/daily/monthly counters never accumulate.
    """
    return encode(
        bytes(
            [
                CMD_SET_DATETIME,
                0x00,
                dt.second,
                dt.minute,
                dt.hour,
                dt.day,
                dt.month,
                (dt.year >> 8) & 0xFF,
                dt.year & 0xFF,
                0x00,
                0x00,
            ]
        )
    )


def cmd_set_led(on: bool) -> bytes:
    return encode(bytes([CMD_MISC, 0x00, 0x05, 1 if on else 0]) + bytes(4))


def cmd_change_pin(new_pin: str, old_pin: str) -> bytes:
    """Change the login PIN.

    Note: the published example for this command carries a checksum copied
    from the authorize example and does not match its own payload. The
    ``(sum + 1) & 0xff`` rule, which every other documented frame satisfies,
    is used here instead.
    """
    return encode(
        bytes([CMD_AUTH, 0x00, 0x01]) + _pin_bytes(new_pin) + _pin_bytes(old_pin)
    )


def cmd_reset_pin() -> bytes:
    """Reset the PIN to 0000. Same checksum caveat as :func:`cmd_change_pin`."""
    return encode(bytes([CMD_AUTH, 0x00, 0x02]) + bytes(8))


def cmd_set_power_limit(watts: int) -> bytes:
    """Set the overload cutoff, in watts."""
    if not 0 <= watts <= 0xFFFF:
        raise ValueError(f"power limit out of range: {watts}")
    return encode(
        bytes([CMD_SET_OVERLOAD, 0x00])
        + watts.to_bytes(2, "big")
        + bytes(2)
    )


def _pin_bytes(pin: str) -> bytes:
    if len(pin) != 4 or not pin.isdigit():
        raise ValueError(f"PIN must be exactly 4 digits, got {pin!r}")
    return bytes(int(d) for d in pin)


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Measurement:
    """A single instantaneous reading.

    ``energy_wh`` is the device's lifetime counter. It is **always 0 on
    hardware < 3**, which does not implement it; use the history queries or
    :class:`sem6000.energy.Integrator` instead. ``energy_available`` says
    which case you are in.
    """

    on: bool
    power_w: float
    voltage_v: int
    current_a: float
    frequency_hz: int
    energy_wh: int
    energy_available: bool

    @property
    def apparent_va(self) -> float:
        return self.voltage_v * self.current_a

    @property
    def power_factor(self) -> Optional[float]:
        va = self.apparent_va
        if va <= 0:
            return None
        return min(self.power_w / va, 1.0)


def parse_measurement(payload: bytes, *, hardware_version: int = 2) -> Measurement:
    """Decode a 0x04 measurement payload.

    Layout (offsets into the payload, i.e. after the 0f/len header)::

        0     command 0x04
        1     0x00
        2     relay state, 1 = on
        3:6   power, milliwatts, 3 bytes
        6     voltage, volts
        7:9   current, milliamps, 2 bytes
        9     frequency, Hz
        10:14 lifetime energy, Wh, 4 bytes (always 0 on hardware < 3)
    """
    if len(payload) < 14:
        raise ProtocolError(f"measurement payload too short: {payload.hex(' ')}")
    if payload[0] != CMD_MEASURE:
        raise ProtocolError(f"not a measurement payload: {payload.hex(' ')}")

    return Measurement(
        on=bool(payload[2]),
        power_w=int.from_bytes(payload[3:6], "big") / 1000,
        voltage_v=payload[6],
        current_a=int.from_bytes(payload[7:9], "big") / 1000,
        frequency_hz=payload[9],
        energy_wh=int.from_bytes(payload[10:14], "big"),
        energy_available=hardware_version >= 3,
    )


@dataclass(frozen=True)
class Settings:
    led_on: bool
    power_limit_w: int
    price_per_kwh: float
    reduced_price_per_kwh: float
    reduced_mode_active: bool
    reduced_start_minute: int
    reduced_end_minute: int


def parse_settings(payload: bytes) -> Settings:
    """Decode a 0x10 settings payload."""
    if len(payload) < 13 or payload[0] != CMD_SETTINGS:
        raise ProtocolError(f"not a settings payload: {payload.hex(' ')}")
    return Settings(
        reduced_mode_active=bool(payload[2]),
        price_per_kwh=payload[3] / 100.0,
        reduced_price_per_kwh=payload[4] / 100.0,
        reduced_start_minute=int.from_bytes(payload[5:7], "big"),
        reduced_end_minute=int.from_bytes(payload[7:9], "big"),
        led_on=bool(payload[9]),
        power_limit_w=int.from_bytes(payload[11:13], "big"),
    )


def parse_ack(payload: bytes, expected_cmd: int) -> bool:
    """Decode a short acknowledgement; True when the device reported success."""
    if len(payload) < 3 or payload[0] != expected_cmd:
        raise ProtocolError(
            f"expected ack for {expected_cmd:#04x}, got {payload.hex(' ')}"
        )
    return payload[2] == 0


def parse_serial(payload: bytes) -> str:
    if payload[0] != CMD_SERIAL:
        raise ProtocolError(f"not a serial payload: {payload.hex(' ')}")
    raw = payload[2:18]
    return "".join(chr(b) for b in raw if 32 <= b < 127)


@dataclass(frozen=True)
class DeviceInfo:
    vendor: str
    firmware: str
    hardware: str

    @property
    def hardware_major(self) -> int:
        return int(self.hardware.split(".")[0])


def parse_device_info(raw: bytes) -> DeviceInfo:
    """Decode the fff1 characteristic (a plain read, not a framed message)."""
    if len(raw) < 15:
        raise ProtocolError(f"device info too short: {raw.hex(' ')}")
    return DeviceInfo(
        vendor=raw[:6].decode("ascii", "replace"),
        firmware=f"{raw[11]}.{raw[12]}",
        hardware=f"{raw[13]}.{raw[14]}",
    )


def parse_history(payload: bytes, kind: int) -> List[int]:
    """Decode a history response into a list of Wh values, oldest first.

    Record widths differ per resolution::

        0x0a  hourly    24 records x 2 bytes
        0x0b  daily     30 records x 4 bytes (3-byte value + 1 pad)
        0x0c  monthly   12 records x 4 bytes (3-byte value + 1 pad)

    The last element is always the current hour/day/month, which is still
    accumulating.
    """
    if payload[0] != kind:
        raise ProtocolError(
            f"history kind mismatch: wanted {kind:#04x}, got {payload[0]:#04x}"
        )
    body = payload[2:]

    if kind == CMD_HISTORY_HOURLY:
        count, width, value_len = 24, 2, 2
    elif kind == CMD_HISTORY_DAILY:
        count, width, value_len = 30, 4, 3
    else:
        count, width, value_len = 12, 4, 3

    out: List[int] = []
    for i in range(count):
        rec = body[i * width : i * width + width]
        if len(rec) < value_len:
            break
        out.append(int.from_bytes(rec[:value_len], "big"))
    return out
