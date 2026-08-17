"""Measure what a workload actually costs at the wall.

Records a quiet baseline, runs the workload while sampling the plug, then
reports the energy attributable to the workload itself.

    python examples/benchmark.py --address AA:BB:CC:DD:EE:FF -- stress-ng --cpu 8

With no command it burns every core for the duration instead.
"""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sem6000 import SEM6000  # noqa: E402
from sem6000.energy import measure_energy  # noqa: E402


def _burn(stop_at: float) -> None:
    x = 0
    while time.monotonic() < stop_at:
        x = (x * x + 1) % 1_000_003


def _spawn_load(seconds: float, workers: int):
    stop_at = time.monotonic() + seconds
    procs = [mp.Process(target=_burn, args=(stop_at,)) for _ in range(workers)]
    for p in procs:
        p.start()
    return procs


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default=os.environ.get("SEM6000_ADDRESS"))
    ap.add_argument("--pin", default=os.environ.get("SEM6000_PIN", "0000"))
    ap.add_argument("--baseline", type=float, default=15.0)
    ap.add_argument("--load", type=float, default=30.0)
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    if not args.address:
        print("need --address or $SEM6000_ADDRESS", file=sys.stderr)
        return 2

    async with SEM6000(args.address, args.pin) as plug:
        info = plug.info
        print(f"plug: {info.vendor} fw {info.firmware} hw {info.hardware}\n")

        print(f"baseline for {args.baseline:.0f}s (keep the machine quiet)...")
        base, _ = await measure_energy(
            plug, args.baseline, interval_s=args.interval, on_sample=_tick
        )
        print(f"\n  idle mean {base.mean_w:.2f} W  peak {base.peak_w:.2f} W\n")

        print(f"loading {args.workers} workers for {args.load:.0f}s...")
        procs = _spawn_load(args.load, args.workers)
        try:
            load, _ = await measure_energy(
                plug, args.load, interval_s=args.interval, on_sample=_tick
            )
        finally:
            for p in procs:
                p.join(timeout=5)
        print(f"\n  load mean {load.mean_w:.2f} W  peak {load.peak_w:.2f} W\n")

        delta_w = load.mean_w - base.mean_w
        attributable_wh = delta_w * load.elapsed_s / 3600.0

        print("--- result ---")
        print(f"idle           {base.mean_w:8.2f} W")
        print(f"under load     {load.mean_w:8.2f} W")
        print(f"workload cost  {delta_w:8.2f} W above idle")
        print(f"total energy   {load.energy_wh:8.3f} Wh over {load.elapsed_s:.0f}s")
        print(f"attributable   {attributable_wh:8.3f} Wh to the workload")
        if delta_w > 0:
            hours = 1000.0 / delta_w
            print(f"extrapolated   {delta_w * 24 / 1000:8.3f} kWh/day if sustained")
            print(f"               {hours:8.1f} h of load per kWh")
    return 0


def _tick(s) -> None:
    print(f"\r  {s.power_w:8.3f} W", end="", flush=True)


if __name__ == "__main__":
    mp.set_start_method("fork", force=True)
    sys.exit(asyncio.run(main()))
