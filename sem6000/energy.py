"""Client-side energy accumulation.

Hardware revisions below 3 leave the device's lifetime energy counter at zero,
and the stored history is only granular to the hour. When you need energy over
a short window - benchmarking a process, say - integrate the power readings
yourself.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple


@dataclass
class Integrator:
    """Accumulates energy from a series of power samples.

    Uses the trapezoidal rule, which handles the ramps at the start and end of
    a load far better than treating each sample as a rectangle::

        it = Integrator()
        it.add(20.0)
        it.add(60.0)          # 40 W average over the gap
        print(it.energy_wh)

    ``time_source`` is injectable so tests need not sleep.
    """

    time_source: Callable[[], float] = time.monotonic

    energy_wh: float = 0.0
    samples: int = 0
    _last: Optional[Tuple[float, float]] = field(default=None, repr=False)
    _start: Optional[float] = field(default=None, repr=False)
    _peak_w: float = 0.0
    _sum_w: float = 0.0

    def add(self, power_w: float, at: Optional[float] = None) -> None:
        """Record a power sample, in watts."""
        if power_w < 0:
            raise ValueError(f"power must not be negative, got {power_w}")
        now = self.time_source() if at is None else at

        if self._last is not None:
            prev_t, prev_w = self._last
            dt_h = (now - prev_t) / 3600.0
            if dt_h < 0:
                raise ValueError("samples must be non-decreasing in time")
            self.energy_wh += (prev_w + power_w) / 2.0 * dt_h
        else:
            self._start = now

        self._last = (now, power_w)
        self.samples += 1
        self._peak_w = max(self._peak_w, power_w)
        self._sum_w += power_w

    @property
    def elapsed_s(self) -> float:
        if self._start is None or self._last is None:
            return 0.0
        return self._last[0] - self._start

    @property
    def peak_w(self) -> float:
        return self._peak_w

    @property
    def mean_w(self) -> float:
        """Time-weighted mean power over the window."""
        if self.elapsed_s <= 0:
            return self._sum_w / self.samples if self.samples else 0.0
        return self.energy_wh * 3600.0 / self.elapsed_s

    @property
    def energy_kwh(self) -> float:
        return self.energy_wh / 1000.0

    @property
    def energy_joules(self) -> float:
        return self.energy_wh * 3600.0

    def cost(self, price_per_kwh: float) -> float:
        return self.energy_kwh * price_per_kwh

    def reset(self) -> None:
        self.energy_wh = 0.0
        self.samples = 0
        self._last = None
        self._start = None
        self._peak_w = 0.0
        self._sum_w = 0.0


@dataclass
class Sample:
    t: float
    power_w: float
    voltage_v: int
    current_a: float


async def measure_energy(
    plug,
    duration_s: float,
    *,
    interval_s: float = 1.0,
    keep_samples: bool = False,
    on_sample: Optional[Callable[[Sample], None]] = None,
) -> Tuple[Integrator, List[Sample]]:
    """Poll ``plug`` for ``duration_s`` and integrate the power readings.

    Returns the integrator and, when ``keep_samples`` is set, the raw samples.
    The device updates its own figures roughly once a second, so intervals
    much below that buy no extra accuracy.
    """
    it = Integrator()
    samples: List[Sample] = []
    loop = asyncio.get_running_loop()
    end = loop.time() + duration_s

    while True:
        m = await plug.measure()
        now = loop.time()
        it.add(m.power_w, at=now)

        s = Sample(t=now, power_w=m.power_w, voltage_v=m.voltage_v,
                   current_a=m.current_a)
        if keep_samples:
            samples.append(s)
        if on_sample is not None:
            on_sample(s)

        if now >= end:
            break
        await asyncio.sleep(min(interval_s, max(0.0, end - now)))

    return it, samples
