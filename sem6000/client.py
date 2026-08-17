"""Async BLE client for the Voltcraft SEM6000."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from typing import List, Optional

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDeviceNotFoundError

from . import protocol as p
from .protocol import (
    DeviceInfo,
    Measurement,
    ProtocolError,
    Settings,
)

log = logging.getLogger(__name__)

DEFAULT_PIN = "0000"

# The device answers within a few hundred ms; a long history response spans
# several notifications and needs a little more room.
_TIMEOUT = 6.0
_HISTORY_TIMEOUT = 10.0

# The relay flips immediately but the power figure lags roughly one sampling
# window, so a reading taken right after a switch still shows the old load.
SWITCH_SETTLE_S = 2.0


class SEM6000Error(Exception):
    """Base class for client-level failures."""


class AuthenticationError(SEM6000Error):
    """The device rejected the PIN."""


class NotConnectedError(SEM6000Error):
    """Operation attempted outside an active connection."""


class SEM6000:
    """Talks to one SEM6000 smart plug.

    Use as an async context manager, which connects and authenticates::

        async with SEM6000("AA:BB:CC:DD:EE:FF") as plug:
            print(await plug.measure())
            await plug.switch(True)

    Every command must be preceded by a successful login, and the login only
    lasts for the lifetime of the BLE connection, so :meth:`connect` always
    authenticates before returning.
    """

    def __init__(
        self,
        address: str,
        pin: str = DEFAULT_PIN,
        *,
        timeout: float = 30.0,
        auto_disconnect_stale: bool = True,
    ) -> None:
        self.address = address
        self.pin = pin
        self.timeout = timeout
        self.auto_disconnect_stale = auto_disconnect_stale

        self._client: Optional[BleakClient] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._reassembler = p.Reassembler()
        self._info: Optional[DeviceInfo] = None
        self._lock = asyncio.Lock()

    # -- connection --------------------------------------------------------

    async def __aenter__(self) -> "SEM6000":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def info(self) -> Optional[DeviceInfo]:
        """Vendor/firmware/hardware, populated once connected."""
        return self._info

    async def connect(self) -> None:
        if self.is_connected:
            return

        device = await self._resolve_device()
        client = BleakClient(device, timeout=self.timeout)
        await client.connect()
        self._client = client

        self._reassembler.reset()
        self._drain()
        await client.start_notify(p.CHAR_NOTIFY, self._on_notify)

        raw = await client.read_gatt_char(p.CHAR_INFO)
        self._info = p.parse_device_info(raw)
        log.debug("connected to %s (%s)", self.address, self._info)

        await self._authenticate()

    async def _resolve_device(self):
        """Find the device, working around BlueZ's already-connected blind spot.

        A connected peripheral stops advertising, so bleak's scan-based lookup
        cannot see it and raises "device not found" - even though the device is
        right there and BlueZ already has its GATT table. Dropping the stale
        link makes it advertise again.
        """
        device = await BleakScanner.find_device_by_address(
            self.address, timeout=self.timeout
        )
        if device is not None:
            return device

        if self.auto_disconnect_stale and self._drop_stale_connection():
            device = await BleakScanner.find_device_by_address(
                self.address, timeout=self.timeout
            )
            if device is not None:
                return device

        raise BleakDeviceNotFoundError(
            f"{self.address} not found. Check it is powered and in range; if "
            f"another process holds the connection, close it first."
        )

    def _drop_stale_connection(self) -> bool:
        """Ask BlueZ to disconnect the device. Linux only; best effort."""
        if not sys.platform.startswith("linux"):
            return False
        btctl = shutil.which("bluetoothctl")
        if not btctl:
            return False
        log.debug("dropping stale BlueZ connection to %s", self.address)
        try:
            subprocess.run(
                [btctl, "disconnect", self.address],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return False
        return True

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            if self._client.is_connected:
                await self._client.stop_notify(p.CHAR_NOTIFY)
                await self._client.disconnect()
        except Exception as exc:  # pragma: no cover - teardown is best effort
            log.debug("error during disconnect: %s", exc)
        finally:
            self._client = None

    # -- transport ---------------------------------------------------------

    def _on_notify(self, _sender, data: bytearray) -> None:
        for frame in self._reassembler.feed(bytes(data)):
            self._queue.put_nowait(frame)

    def _drain(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    async def _request(
        self, command: bytes, expect: int, *, timeout: float = _TIMEOUT
    ) -> bytes:
        """Send a framed command and return the payload of its response.

        Responses are matched on the command byte rather than taken blindly in
        arrival order, so an unsolicited or late notification cannot shift
        every subsequent reply by one.
        """
        if not self.is_connected:
            raise NotConnectedError("not connected - call connect() first")

        async with self._lock:
            self._drain()
            await self._client.write_gatt_char(p.CHAR_WRITE, command, response=False)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"no response to command {expect:#04x} after {timeout}s"
                    )
                try:
                    frame = await asyncio.wait_for(self._queue.get(), remaining)
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        f"no response to command {expect:#04x} after {timeout}s"
                    ) from None

                try:
                    payload = p.decode(frame)
                except ProtocolError as exc:
                    log.warning("discarding bad frame: %s", exc)
                    continue

                if payload and payload[0] == expect:
                    return payload
                log.debug(
                    "ignoring frame for %#04x while awaiting %#04x",
                    payload[0] if payload else -1,
                    expect,
                )

    async def _authenticate(self, attempts: int = 3) -> None:
        """Log in with the PIN.

        The device intermittently ignores the very first command after a fresh
        connection, so a couple of retries are expected rather than an error.
        """
        last: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                payload = await self._request(p.cmd_authorize(self.pin), p.CMD_AUTH)
            except (TimeoutError, ProtocolError) as exc:
                last = exc
                log.debug("auth attempt %d failed: %s", attempt + 1, exc)
                await asyncio.sleep(0.2 * (attempt + 1))
                continue

            if p.parse_ack(payload, p.CMD_AUTH):
                return
            raise AuthenticationError(
                f"device rejected PIN {self.pin!r}. Reset it by holding the "
                f"plug's button, or pass the correct pin=."
            )

        raise SEM6000Error(f"authentication did not complete: {last}")

    # -- public API --------------------------------------------------------

    async def measure(self) -> Measurement:
        """Read instantaneous power, voltage, current, frequency and relay state."""
        hw = self._info.hardware_major if self._info else 2
        payload = await self._request(p.cmd_measure(), p.CMD_MEASURE)
        return p.parse_measurement(payload, hardware_version=hw)

    async def is_on(self) -> bool:
        """Read just the relay state."""
        return (await self.measure()).on

    async def switch(self, on: bool, *, settle: bool = False) -> None:
        """Turn the socket on or off.

        The relay state reported by :meth:`measure` updates immediately, but the
        power figure lags about one sampling window. Pass ``settle=True`` to
        wait that out when the next thing you do is read power.
        """
        payload = await self._request(p.cmd_switch(on), p.CMD_SWITCH)
        if not p.parse_ack(payload, p.CMD_SWITCH):
            raise SEM6000Error(f"device refused to switch {'on' if on else 'off'}")
        if settle:
            await asyncio.sleep(SWITCH_SETTLE_S)

    async def turn_on(self, **kw) -> None:
        await self.switch(True, **kw)

    async def turn_off(self, **kw) -> None:
        await self.switch(False, **kw)

    async def toggle(self, **kw) -> bool:
        """Flip the relay; returns the new state."""
        new = not await self.is_on()
        await self.switch(new, **kw)
        return new

    async def settings(self) -> Settings:
        """Read LED, overload limit and tariff configuration."""
        return p.parse_settings(await self._request(p.cmd_settings(), p.CMD_SETTINGS))

    async def serial(self) -> str:
        return p.parse_serial(await self._request(p.cmd_serial(), p.CMD_SERIAL))

    async def set_led(self, on: bool) -> None:
        await self._request(p.cmd_set_led(on), p.CMD_MISC)

    async def set_power_limit(self, watts: int) -> None:
        """Set the overload cutoff.

        The plug cuts power when the load exceeds this. Do not raise it above
        the socket's rating - 2300 W for the 10 A CH version.
        """
        payload = await self._request(
            p.cmd_set_power_limit(watts), p.CMD_SET_OVERLOAD
        )
        if not p.parse_ack(payload, p.CMD_SET_OVERLOAD):
            raise SEM6000Error(f"device refused power limit {watts} W")

    async def change_pin(self, new_pin: str) -> None:
        """Change the login PIN from the current one to ``new_pin``.

        The client keeps using the new PIN for subsequent reconnects. Write it
        down: recovering from a forgotten PIN needs a physical reset.
        """
        payload = await self._request(
            p.cmd_change_pin(new_pin, self.pin), p.CMD_AUTH
        )
        if not p.parse_ack(payload, p.CMD_AUTH):
            raise SEM6000Error("device refused the PIN change")
        self.pin = new_pin

    async def reset_pin(self) -> None:
        """Reset the PIN back to the 0000 default."""
        payload = await self._request(p.cmd_reset_pin(), p.CMD_AUTH)
        if not p.parse_ack(payload, p.CMD_AUTH):
            raise SEM6000Error("device refused the PIN reset")
        self.pin = DEFAULT_PIN

    async def sync_time(self, when: Optional[datetime] = None) -> None:
        """Set the device clock to ``when`` (default: now, local time).

        The plug files its stored energy into hourly/daily/monthly buckets
        using its own clock, and it does not keep time across a power cut. If
        the clock has never been set, the history queries return all zeros.
        Call this once after the plug loses power.
        """
        when = when or datetime.now()
        payload = await self._request(p.cmd_set_datetime(when), p.CMD_SET_DATETIME)
        if not p.parse_ack(payload, p.CMD_SET_DATETIME):
            raise SEM6000Error("device rejected the clock update")

    # -- stored energy history --------------------------------------------

    async def _history(self, kind: int) -> List[int]:
        payload = await self._request(
            p.cmd_history(kind), kind, timeout=_HISTORY_TIMEOUT
        )
        return p.parse_history(payload, kind)

    async def history_hourly(self) -> List[int]:
        """Wh for each of the last 24 hours, oldest first.

        The final entry is the hour in progress and is still accumulating.
        """
        return await self._history(p.CMD_HISTORY_HOURLY)

    async def history_daily(self) -> List[int]:
        """Wh for each of the last 30 days, oldest first; last entry is today."""
        return await self._history(p.CMD_HISTORY_DAILY)

    async def history_monthly(self) -> List[int]:
        """Wh for each of the last 12 months, oldest first; last is this month."""
        return await self._history(p.CMD_HISTORY_MONTHLY)

    async def energy_today_wh(self) -> int:
        """Energy used so far today, in Wh."""
        return (await self.history_daily())[-1]

    async def energy_this_month_wh(self) -> int:
        return (await self.history_monthly())[-1]


async def discover(timeout: float = 10.0) -> List[dict]:
    """Scan for SEM6000 plugs in range.

    Returns dicts with ``address``, ``name`` and ``rssi``. Note that a plug
    already connected to this machine will not appear, because connected
    peripherals stop advertising.
    """
    found = []
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for address, (device, adv) in devices.items():
        name = device.name or ""
        if "voltcraft" in name.lower() or p.SERVICE_UUID in (adv.service_uuids or []):
            found.append({"address": address, "name": name, "rssi": adv.rssi})
    return found
