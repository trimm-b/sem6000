"""Long-running sampler that records measurements to disk.

Hardware below revision 3 has no lifetime energy counter, and the device's own
history only resolves to the hour. For real long-term tracking you have to keep
the record yourself - that is what this does.

    python -m sem6000 log --sqlite power.db --interval 5

Energy is integrated between consecutive samples with the trapezoidal rule and
stored per row, so summing the ``energy_wh`` column over any time range gives
the energy for that range regardless of sampling gaps.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

from .client import SEM6000

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Row:
    timestamp: datetime
    on: bool
    power_w: float
    voltage_v: int
    current_a: float
    frequency_hz: int
    energy_wh: float  # energy accumulated since the previous row


class Sink(Protocol):
    def write(self, row: Row) -> None: ...
    def close(self) -> None: ...


class CsvSink:
    """Appends rows to a CSV file, writing a header only for a new file."""

    FIELDS = [
        "timestamp",
        "on",
        "power_w",
        "voltage_v",
        "current_a",
        "frequency_hz",
        "energy_wh",
    ]

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        self._fh = self.path.open("a", newline="")
        self._writer = csv.writer(self._fh)
        if is_new:
            self._writer.writerow(self.FIELDS)
            self._fh.flush()

    def write(self, row: Row) -> None:
        self._writer.writerow(
            [
                row.timestamp.isoformat(),
                int(row.on),
                f"{row.power_w:.3f}",
                row.voltage_v,
                f"{row.current_a:.3f}",
                row.frequency_hz,
                f"{row.energy_wh:.6f}",
            ]
        )
        self._fh.flush()  # survive a kill -9

    def close(self) -> None:
        self._fh.close()


class SqliteSink:
    """Appends rows to a SQLite table, creating it if needed."""

    def __init__(self, path: Path, table: str = "measurements") -> None:
        self.table = table
        self._db = sqlite3.connect(str(path))
        self._db.execute(
            f"""CREATE TABLE IF NOT EXISTS {table} (
                    timestamp     TEXT    NOT NULL,
                    on_state      INTEGER NOT NULL,
                    power_w       REAL    NOT NULL,
                    voltage_v     INTEGER NOT NULL,
                    current_a     REAL    NOT NULL,
                    frequency_hz  INTEGER NOT NULL,
                    energy_wh     REAL    NOT NULL
                )"""
        )
        self._db.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_ts ON {table}(timestamp)"
        )
        self._db.commit()

    def write(self, row: Row) -> None:
        self._db.execute(
            f"INSERT INTO {self.table} VALUES (?,?,?,?,?,?,?)",
            (
                row.timestamp.isoformat(),
                int(row.on),
                row.power_w,
                row.voltage_v,
                row.current_a,
                row.frequency_hz,
                row.energy_wh,
            ),
        )
        self._db.commit()

    def total_wh(self, since: Optional[datetime] = None) -> float:
        q = f"SELECT COALESCE(SUM(energy_wh), 0) FROM {self.table}"
        params: tuple = ()
        if since is not None:
            q += " WHERE timestamp >= ?"
            params = (since.isoformat(),)
        return float(self._db.execute(q, params).fetchone()[0])

    def close(self) -> None:
        self._db.close()


class StdoutSink:
    def write(self, row: Row) -> None:
        state = "ON " if row.on else "OFF"
        print(
            f"{row.timestamp.strftime('%H:%M:%S')}  {state}  "
            f"{row.power_w:8.3f} W  {row.voltage_v:3d} V  "
            f"{row.current_a:6.3f} A  +{row.energy_wh:.4f} Wh",
            flush=True,
        )

    def close(self) -> None:
        pass


async def run_logger(
    plug: SEM6000,
    sinks: list,
    *,
    interval_s: float = 5.0,
    duration_s: Optional[float] = None,
    reconnect: bool = True,
) -> float:
    """Sample ``plug`` into ``sinks`` until stopped. Returns total Wh recorded.

    Survives dropped connections when ``reconnect`` is set: BLE links fail
    routinely over hours, and a logger that dies on the first blip is useless.
    """
    total_wh = 0.0
    prev_t: Optional[float] = None
    prev_w: Optional[float] = None
    started = time.monotonic()
    backoff = 1.0

    while True:
        if duration_s is not None and time.monotonic() - started >= duration_s:
            break

        try:
            if not plug.is_connected:
                log.info("reconnecting to %s", plug.address)
                await plug.connect()
                # The gap while disconnected is unmeasured; do not integrate
                # across it or the missing time is invented as energy.
                prev_t = prev_w = None
            backoff = 1.0

            m = await plug.measure()
            now = time.monotonic()

            energy = 0.0
            if prev_t is not None and prev_w is not None:
                energy = (prev_w + m.power_w) / 2.0 * (now - prev_t) / 3600.0
                total_wh += energy
            prev_t, prev_w = now, m.power_w

            row = Row(
                timestamp=datetime.now(timezone.utc).astimezone(),
                on=m.on,
                power_w=m.power_w,
                voltage_v=m.voltage_v,
                current_a=m.current_a,
                frequency_hz=m.frequency_hz,
                energy_wh=energy,
            )
            for sink in sinks:
                sink.write(row)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not reconnect:
                raise
            log.warning("sample failed (%s); retrying in %.0fs", exc, backoff)
            prev_t = prev_w = None
            try:
                await plug.disconnect()
            except Exception:
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue

        await asyncio.sleep(interval_s)

    return total_wh
