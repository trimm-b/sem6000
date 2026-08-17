import csv
import sqlite3
from datetime import datetime, timedelta

import pytest

from sem6000.logger import CsvSink, Row, SqliteSink


def make_row(power=100.0, energy=0.5, on=True):
    return Row(
        timestamp=datetime(2026, 8, 17, 23, 30, 0),
        on=on,
        power_w=power,
        voltage_v=231,
        current_a=0.43,
        frequency_hz=50,
        energy_wh=energy,
    )


def test_csv_writes_header_once(tmp_path):
    path = tmp_path / "p.csv"
    CsvSink(path).write(make_row())
    # Reopening an existing file must not repeat the header.
    CsvSink(path).write(make_row(power=200.0))

    rows = list(csv.reader(path.open()))
    assert rows[0] == CsvSink.FIELDS
    assert len(rows) == 3
    assert rows[1][2] == "100.000" and rows[2][2] == "200.000"


def test_csv_round_trips_values(tmp_path):
    path = tmp_path / "p.csv"
    CsvSink(path).write(make_row(on=False))
    row = next(csv.DictReader(path.open()))
    assert row["on"] == "0"
    assert row["voltage_v"] == "231"
    assert float(row["energy_wh"]) == pytest.approx(0.5)


def test_sqlite_creates_schema_and_inserts(tmp_path):
    db = tmp_path / "p.db"
    sink = SqliteSink(db)
    sink.write(make_row())
    sink.close()

    con = sqlite3.connect(db)
    (count,) = con.execute("SELECT COUNT(*) FROM measurements").fetchone()
    assert count == 1


def test_sqlite_total_sums_energy(tmp_path):
    sink = SqliteSink(tmp_path / "p.db")
    for _ in range(4):
        sink.write(make_row(energy=0.25))
    assert sink.total_wh() == pytest.approx(1.0)


def test_sqlite_total_respects_since(tmp_path):
    sink = SqliteSink(tmp_path / "p.db")
    base = datetime(2026, 8, 17, 12, 0, 0)
    for i in range(4):
        sink.write(
            Row(
                timestamp=base + timedelta(hours=i),
                on=True,
                power_w=10.0,
                voltage_v=231,
                current_a=0.1,
                frequency_hz=50,
                energy_wh=1.0,
            )
        )
    assert sink.total_wh(since=base + timedelta(hours=2)) == pytest.approx(2.0)


def test_sqlite_reopen_appends(tmp_path):
    db = tmp_path / "p.db"
    s1 = SqliteSink(db)
    s1.write(make_row())
    s1.close()

    s2 = SqliteSink(db)
    s2.write(make_row())
    assert s2.total_wh() == pytest.approx(1.0)
