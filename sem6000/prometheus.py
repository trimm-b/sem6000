"""Prometheus exporter.

Serves the text exposition format from the standard library alone - no
prometheus_client dependency:

    python -m sem6000 export --port 9110

Then scrape http://localhost:9110/metrics.

The plug is polled on a timer in the background rather than on scrape, so a
slow BLE round-trip can never stall Prometheus, and several scrapers share one
connection.
"""

from __future__ import annotations

import asyncio
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Optional

from .client import SEM6000
from .protocol import Measurement

log = logging.getLogger(__name__)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class _State:
    """Latest reading, shared between the poller and the HTTP threads."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.measurement: Optional[Measurement] = None
        self.updated_at: float = 0.0
        self.energy_wh: float = 0.0
        self.scrape_errors: int = 0
        self.up: int = 0

    def set(self, m: Measurement, energy_wh: float) -> None:
        with self._lock:
            self.measurement = m
            self.energy_wh = energy_wh
            self.updated_at = time.time()
            self.up = 1

    def fail(self) -> None:
        with self._lock:
            self.scrape_errors += 1
            self.up = 0

    def render(self, address: str) -> str:
        with self._lock:
            m = self.measurement
            lines = [
                "# HELP sem6000_up Whether the last poll of the plug succeeded.",
                "# TYPE sem6000_up gauge",
                f'sem6000_up{{address="{address}"}} {self.up}',
                "# HELP sem6000_poll_errors_total Failed polls since start.",
                "# TYPE sem6000_poll_errors_total counter",
                f'sem6000_poll_errors_total{{address="{address}"}} '
                f"{self.scrape_errors}",
            ]
            if m is None:
                return "\n".join(lines) + "\n"

            labels = f'{{address="{address}"}}'
            lines += [
                "# HELP sem6000_socket_on Relay state, 1 = on.",
                "# TYPE sem6000_socket_on gauge",
                f"sem6000_socket_on{labels} {int(m.on)}",
                "# HELP sem6000_power_watts Real power drawn by the load.",
                "# TYPE sem6000_power_watts gauge",
                f"sem6000_power_watts{labels} {m.power_w}",
                "# HELP sem6000_voltage_volts Mains voltage.",
                "# TYPE sem6000_voltage_volts gauge",
                f"sem6000_voltage_volts{labels} {m.voltage_v}",
                "# HELP sem6000_current_amperes Load current.",
                "# TYPE sem6000_current_amperes gauge",
                f"sem6000_current_amperes{labels} {m.current_a}",
                "# HELP sem6000_frequency_hertz Mains frequency.",
                "# TYPE sem6000_frequency_hertz gauge",
                f"sem6000_frequency_hertz{labels} {m.frequency_hz}",
                "# HELP sem6000_apparent_voltamperes Apparent power.",
                "# TYPE sem6000_apparent_voltamperes gauge",
                f"sem6000_apparent_voltamperes{labels} {m.apparent_va:.3f}",
                "# HELP sem6000_energy_watthours_total Energy integrated since "
                "this exporter started.",
                "# TYPE sem6000_energy_watthours_total counter",
                f"sem6000_energy_watthours_total{labels} {self.energy_wh:.6f}",
                "# HELP sem6000_last_poll_timestamp_seconds Unix time of the "
                "last successful poll.",
                "# TYPE sem6000_last_poll_timestamp_seconds gauge",
                f"sem6000_last_poll_timestamp_seconds{labels} {self.updated_at:.0f}",
            ]
            if m.power_factor is not None:
                lines += [
                    "# HELP sem6000_power_factor Real power over apparent power.",
                    "# TYPE sem6000_power_factor gauge",
                    f"sem6000_power_factor{labels} {m.power_factor:.4f}",
                ]
            return "\n".join(lines) + "\n"


def _make_handler(state: _State, address: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
            if self.path in ("/metrics", "/"):
                body = state.render(address).encode()
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass  # keep scrape traffic out of stdout

    return Handler


async def serve(
    plug: SEM6000,
    *,
    port: int = 9110,
    host: str = "0.0.0.0",
    interval_s: float = 5.0,
) -> None:
    """Poll ``plug`` and serve its metrics until cancelled."""
    state = _State()
    server = ThreadingHTTPServer((host, port), _make_handler(state, plug.address))
    Thread(target=server.serve_forever, name="sem6000-http", daemon=True).start()
    log.info("serving metrics on http://%s:%d/metrics", host, port)

    energy_wh = 0.0
    prev_t = prev_w = None
    backoff = 1.0
    try:
        while True:
            try:
                if not plug.is_connected:
                    await plug.connect()
                    prev_t = prev_w = None
                m = await plug.measure()
                now = time.monotonic()
                if prev_t is not None:
                    energy_wh += (prev_w + m.power_w) / 2.0 * (now - prev_t) / 3600.0
                prev_t, prev_w = now, m.power_w
                state.set(m, energy_wh)
                backoff = 1.0
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("poll failed (%s); retrying in %.0fs", exc, backoff)
                state.fail()
                prev_t = prev_w = None
                try:
                    await plug.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
    finally:
        server.shutdown()
