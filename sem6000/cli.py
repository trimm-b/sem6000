"""Command line interface: python -m sem6000 ..."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import SEM6000, DEFAULT_PIN, discover
from .energy import measure_energy
from .logger import CsvSink, SqliteSink, StdoutSink, run_logger
from .prometheus import serve

ENV_ADDRESS = "SEM6000_ADDRESS"
ENV_PIN = "SEM6000_PIN"


def _fmt_measurement(m) -> str:
    pf = f"{m.power_factor:.2f}" if m.power_factor is not None else "-"
    return (
        f"socket    {'ON' if m.on else 'OFF'}\n"
        f"power     {m.power_w:8.3f} W\n"
        f"voltage   {m.voltage_v:8d} V\n"
        f"current   {m.current_a:8.3f} A\n"
        f"apparent  {m.apparent_va:8.2f} VA\n"
        f"pf        {pf:>8}\n"
        f"frequency {m.frequency_hz:8d} Hz"
    )


def _bar(value: int, peak: int, width: int = 32) -> str:
    if peak <= 0:
        return ""
    return "#" * max(1, round(value / peak * width)) if value else ""


async def _run(args) -> int:
    if args.command == "discover":
        found = await discover(timeout=args.timeout)
        if not found:
            print(
                "No plugs found. A plug already connected to this machine will "
                "not advertise; disconnect it first.",
                file=sys.stderr,
            )
            return 1
        for d in found:
            print(f"{d['address']}  {d['name']:<16} rssi {d['rssi']}")
        return 0

    if not args.address:
        print(
            f"No address given. Pass --address or set {ENV_ADDRESS}. "
            f"Run 'python -m sem6000 discover' to find one.",
            file=sys.stderr,
        )
        return 2

    async with SEM6000(args.address, args.pin) as plug:
        if args.command == "status":
            m = await plug.measure()
            if args.json:
                print(json.dumps(_measurement_dict(m, plug), indent=2))
            else:
                info = plug.info
                print(f"device    {info.vendor} fw {info.firmware} hw {info.hardware}")
                print(_fmt_measurement(m))
            return 0

        if args.command in ("on", "off"):
            await plug.switch(args.command == "on", settle=True)
            m = await plug.measure()
            print(f"socket {'ON' if m.on else 'OFF'}  ({m.power_w:.3f} W)")
            return 0

        if args.command == "toggle":
            state = await plug.toggle(settle=True)
            print(f"socket {'ON' if state else 'OFF'}")
            return 0

        if args.command == "watch":
            try:
                while True:
                    m = await plug.measure()
                    print(
                        f"\r{'ON ' if m.on else 'OFF'}  {m.power_w:8.3f} W  "
                        f"{m.voltage_v:3d} V  {m.current_a:6.3f} A",
                        end="",
                        flush=True,
                    )
                    await asyncio.sleep(args.interval)
            except KeyboardInterrupt:
                print()
            return 0

        if args.command == "energy":
            it, _ = await measure_energy(
                plug,
                args.duration,
                interval_s=args.interval,
                on_sample=None if args.json else _progress,
            )
            if not args.json:
                print()
            result = {
                "duration_s": round(it.elapsed_s, 2),
                "samples": it.samples,
                "energy_wh": round(it.energy_wh, 4),
                "energy_kwh": round(it.energy_kwh, 6),
                "energy_joules": round(it.energy_joules, 1),
                "mean_w": round(it.mean_w, 3),
                "peak_w": round(it.peak_w, 3),
            }
            if args.price is not None:
                result["cost"] = round(it.cost(args.price), 4)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                for k, v in result.items():
                    print(f"{k:14} {v}")
            return 0

        if args.command == "history":
            # Pick the coroutine first, then await it. A dict of awaited calls
            # would run all three queries just to use one.
            fetch = {
                "hour": plug.history_hourly,
                "day": plug.history_daily,
                "month": plug.history_monthly,
            }[args.period]
            data = await fetch()
            if args.json:
                print(json.dumps({args.period: data}))
                return 0
            unit = {"hour": "hour", "day": "day", "month": "month"}[args.period]
            peak = max(data) if data else 0
            for i, wh in enumerate(data):
                ago = len(data) - 1 - i
                label = "now" if ago == 0 else f"-{ago:>2} {unit}"
                print(f"{label:>10}  {wh:6d} Wh  {_bar(wh, peak)}")
            print(f"{'total':>10}  {sum(data):6d} Wh")
            return 0

        if args.command == "settings":
            s = await plug.settings()
            print(f"led           {'on' if s.led_on else 'off'}")
            print(f"power limit   {s.power_limit_w} W")
            print(f"price/kWh     {s.price_per_kwh:.2f}")
            print(f"reduced/kWh   {s.reduced_price_per_kwh:.2f}")
            print(f"serial        {await plug.serial()}")
            return 0

        if args.command == "led":
            await plug.set_led(args.state == "on")
            print(f"led {args.state}")
            return 0

        if args.command == "sync-time":
            await plug.sync_time()
            print("device clock set to local time")
            return 0

        if args.command == "set-limit":
            await plug.set_power_limit(args.watts)
            print(f"overload cutoff set to {args.watts} W")
            return 0

        if args.command == "set-pin":
            if args.new_pin == DEFAULT_PIN:
                print(
                    "refusing to set the default PIN; use 'reset-pin' if that "
                    "is really what you want",
                    file=sys.stderr,
                )
                return 2
            await plug.change_pin(args.new_pin)
            print(
                f"PIN changed to {args.new_pin}. Store it somewhere safe - "
                f"recovering a forgotten PIN needs a physical reset."
            )
            return 0

        if args.command == "reset-pin":
            await plug.reset_pin()
            print(f"PIN reset to the {DEFAULT_PIN} default")
            return 0

        if args.command == "log":
            sinks = []
            if args.csv:
                sinks.append(CsvSink(Path(args.csv)))
            if args.sqlite:
                sinks.append(SqliteSink(Path(args.sqlite)))
            if not sinks or args.stdout:
                sinks.append(StdoutSink())
            try:
                total = await run_logger(
                    plug,
                    sinks,
                    interval_s=args.interval,
                    duration_s=args.duration,
                )
            finally:
                for s in sinks:
                    s.close()
            print(f"\nrecorded {total:.4f} Wh")
            return 0

        if args.command == "export":
            print(
                f"serving metrics on http://{args.host}:{args.port}/metrics "
                f"(ctrl-c to stop)"
            )
            await serve(
                plug, port=args.port, host=args.host, interval_s=args.interval
            )
            return 0

    return 0


def _progress(s) -> None:
    print(f"\r{s.power_w:8.3f} W", end="", flush=True)


def _measurement_dict(m, plug) -> dict:
    return {
        "on": m.on,
        "power_w": m.power_w,
        "voltage_v": m.voltage_v,
        "current_a": m.current_a,
        "apparent_va": round(m.apparent_va, 3),
        "power_factor": round(m.power_factor, 4) if m.power_factor else None,
        "frequency_hz": m.frequency_hz,
        "energy_wh": m.energy_wh,
        "energy_counter_available": m.energy_available,
        "hardware": plug.info.hardware if plug.info else None,
        "firmware": plug.info.firmware if plug.info else None,
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m sem6000",
        description="Control a Voltcraft SEM6000 Bluetooth smart plug.",
    )
    ap.add_argument(
        "--address",
        default=os.environ.get(ENV_ADDRESS),
        help=f"Bluetooth MAC address (default: ${ENV_ADDRESS})",
    )
    ap.add_argument(
        "--pin",
        default=os.environ.get(ENV_PIN, DEFAULT_PIN),
        help=f"4-digit PIN (default: ${ENV_PIN} or {DEFAULT_PIN})",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")

    # Repeat --json on the subcommands so it works in either position;
    # argparse otherwise accepts only "sem6000 --json status", and everyone
    # reaches for "sem6000 status --json" first.
    # SUPPRESS keeps the subcommand from writing a default False over a --json
    # that was given globally; the attribute appears only when actually passed.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )

    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show the current reading", parents=[common])
    sub.add_parser("on", help="switch the socket on")
    sub.add_parser("off", help="switch the socket off")
    sub.add_parser("toggle", help="flip the socket")
    sub.add_parser("settings", help="show device configuration")
    sub.add_parser(
        "sync-time",
        help="set the device clock (required before it will log history)",
    )

    d = sub.add_parser("discover", help="scan for plugs in range")
    d.add_argument("--timeout", type=float, default=10.0)

    w = sub.add_parser("watch", help="stream live readings")
    w.add_argument("--interval", type=float, default=1.0)

    e = sub.add_parser("energy", help="integrate power over a window", parents=[common])
    e.add_argument("duration", type=float, help="seconds to measure")
    e.add_argument("--interval", type=float, default=1.0)
    e.add_argument("--price", type=float, help="price per kWh, to report cost")

    h = sub.add_parser("history", help="stored energy history", parents=[common])
    h.add_argument("period", choices=["hour", "day", "month"])

    led = sub.add_parser("led", help="turn the LED ring on or off")
    led.add_argument("state", choices=["on", "off"])

    lim = sub.add_parser("set-limit", help="set the overload cutoff in watts")
    lim.add_argument("watts", type=int)

    sp = sub.add_parser("set-pin", help="change the login PIN")
    sp.add_argument("new_pin", help="the new 4-digit PIN")

    sub.add_parser("reset-pin", help=f"reset the PIN to {DEFAULT_PIN}")

    lg = sub.add_parser("log", help="record measurements to disk over time")
    lg.add_argument("--csv", help="append rows to this CSV file")
    lg.add_argument("--sqlite", help="append rows to this SQLite database")
    lg.add_argument("--stdout", action="store_true", help="also print each row")
    lg.add_argument("--interval", type=float, default=5.0)
    lg.add_argument("--duration", type=float, help="stop after N seconds")

    ex = sub.add_parser("export", help="serve Prometheus metrics")
    ex.add_argument("--port", type=int, default=9110)
    ex.add_argument("--host", default="0.0.0.0")
    ex.add_argument("--interval", type=float, default=5.0)

    return ap


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
