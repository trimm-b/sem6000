"""Blocking facade over the async client.

For scripts, notebooks and REPL work where an event loop is more ceremony than
it is worth::

    from sem6000 import SEM6000Sync

    with SEM6000Sync("AA:BB:CC:DD:EE:FF") as plug:
        print(plug.measure().power_w)
        plug.turn_off()

The BLE connection has to outlive individual calls, so a private event loop
runs on a background thread for the object's lifetime and each method hands
its coroutine to that loop and waits.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Coroutine, Iterator, List, Optional

from .client import DEFAULT_PIN, SEM6000
from .energy import Integrator
from .protocol import DeviceInfo, Measurement, Settings


class SEM6000Sync:
    """Synchronous wrapper around :class:`~sem6000.client.SEM6000`.

    Not thread-safe: use one instance per thread, or guard it yourself.
    """

    def __init__(
        self,
        address: str,
        pin: str = DEFAULT_PIN,
        *,
        timeout: float = 30.0,
        call_timeout: float = 60.0,
    ) -> None:
        self._plug = SEM6000(address, pin, timeout=timeout)
        self._call_timeout = call_timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name=f"sem6000-{address}", daemon=True
        )
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro: Coroutine) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=self._call_timeout)

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "SEM6000Sync":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        self._call(self._plug.connect())

    def disconnect(self) -> None:
        self._call(self._plug.disconnect())

    def close(self) -> None:
        """Disconnect and shut the background loop down."""
        try:
            self.disconnect()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()

    @property
    def is_connected(self) -> bool:
        return self._plug.is_connected

    @property
    def info(self) -> Optional[DeviceInfo]:
        return self._plug.info

    @property
    def address(self) -> str:
        return self._plug.address

    # -- operations --------------------------------------------------------

    def measure(self) -> Measurement:
        return self._call(self._plug.measure())

    def is_on(self) -> bool:
        return self._call(self._plug.is_on())

    def switch(self, on: bool, *, settle: bool = False) -> None:
        self._call(self._plug.switch(on, settle=settle))

    def turn_on(self, **kw) -> None:
        self.switch(True, **kw)

    def turn_off(self, **kw) -> None:
        self.switch(False, **kw)

    def toggle(self, **kw) -> bool:
        return self._call(self._plug.toggle(**kw))

    def settings(self) -> Settings:
        return self._call(self._plug.settings())

    def serial(self) -> str:
        return self._call(self._plug.serial())

    def set_led(self, on: bool) -> None:
        self._call(self._plug.set_led(on))

    def set_power_limit(self, watts: int) -> None:
        self._call(self._plug.set_power_limit(watts))

    def change_pin(self, new_pin: str) -> None:
        self._call(self._plug.change_pin(new_pin))

    def reset_pin(self) -> None:
        self._call(self._plug.reset_pin())

    def sync_time(self, when: Optional[datetime] = None) -> None:
        self._call(self._plug.sync_time(when))

    def history_hourly(self) -> List[int]:
        return self._call(self._plug.history_hourly())

    def history_daily(self) -> List[int]:
        return self._call(self._plug.history_daily())

    def history_monthly(self) -> List[int]:
        return self._call(self._plug.history_monthly())

    def energy_today_wh(self) -> int:
        return self._call(self._plug.energy_today_wh())

    # -- measuring a block of code ----------------------------------------

    @contextmanager
    def measuring(self, interval_s: float = 1.0) -> Iterator[Integrator]:
        """Measure the energy drawn while a block of code runs::

            with plug.measuring() as energy:
                train_the_model()
            print(f"that cost {energy.energy_wh:.2f} Wh")

        Sampling happens on a background thread. The integrator is filled in
        as it goes and is complete once the block exits.
        """
        it = Integrator()
        stop = threading.Event()

        def poll() -> None:
            while not stop.is_set():
                try:
                    it.add(self.measure().power_w)
                except Exception:
                    # A dropped sample must not take the caller's code with it.
                    pass
                stop.wait(interval_s)

        worker = threading.Thread(target=poll, name="sem6000-sampler", daemon=True)
        worker.start()
        try:
            yield it
        finally:
            stop.set()
            worker.join(timeout=self._call_timeout)
            # A final sample closes the last interval.
            try:
                it.add(self.measure().power_w)
            except Exception:
                pass
