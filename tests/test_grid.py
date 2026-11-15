"""Unit tests for the pure daily grid aggregation (privacy + fixed grid)."""

from __future__ import annotations

import csv
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import grid

DAY = "2026-01-01"
START = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


def gp(mmsi: str, utc: int, lat: float, lon: float) -> grid.GridPoint:
    return grid.GridPoint(mmsi=mmsi, utc=utc, lat=lat, lon=lon)


def test_fixed_grid_boundaries_are_exact():
    assert grid.cell_index(114.299999) == 1142
    assert grid.cell_index(114.3) == 1143
    assert grid.cell_index(0.0) == 0
    assert grid.cell_index(-0.0001) == -1
    assert grid.cell_index(-114.3) == -1143
    assert grid.cell_bounds(225) == (22.5, 22.6)
    assert grid.cell_bounds(-1) == (-0.1, 0.0)


def test_day_bounds_are_half_open_utc_range():
    start, end = grid.utc_day_bounds(DAY)
    assert start == START
    assert end - start == 86400
    assert grid.utc_day_of_timestamp(start) == DAY
    assert grid.utc_day_of_timestamp(end - 1) == DAY
    assert grid.utc_day_of_timestamp(end) == "2026-01-02"


def test_same_vessel_same_cell_counted_once_even_with_many_points():
    points = [gp("9", START + i, 22.50, 114.20) for i in range(10)]
    points += [gp("8", START, 22.55, 114.25), gp("7", START, 22.59, 114.29)]
    cells = grid.aggregate_cells(DAY, points)
    assert len(cells) == 1
    assert cells[0].distinct_vessels == 3


def test_cells_below_three_vessels_are_suppressed():
    points = [
        gp("1", START, 22.5, 114.2),
        gp("2", START, 22.5, 114.2),  # 2 vessels -> hidden
        gp("3", START, 23.0, 115.0),  # 1 vessel -> hidden
        gp("4", START, 24.0, 116.0),
        gp("5", START, 24.01, 116.01),
        gp("6", START, 24.09, 116.09),  # 3 vessels -> shown
    ]
    cells = grid.aggregate_cells(DAY, points)
    assert len(cells) == 1
    assert cells[0].distinct_vessels == 3
    assert (cells[0].lat_index, cells[0].lon_index) == (240, 1160)


def test_points_outside_the_day_are_ignored():
    _, end = grid.utc_day_bounds(DAY)
    points = [
        gp("1", START - 1, 22.5, 114.2),
        gp("2", START, 22.5, 114.2),
        gp("3", START + 100, 22.5, 114.2),
        gp("4", end, 22.5, 114.2),  # first second of next day
        gp("5", end + 999, 22.5, 114.2),
    ]
    # Only vessels 2 and 3 fall inside the day, and with just 2 distinct
    # MMSIs their cell is suppressed, so nothing is publishable.
    assert grid.aggregate_cells(DAY, points) == []


def test_csv_contains_only_publishable_cells_and_no_mmsi():
    points = [
        gp("1", START, 22.5, 114.2),
        gp("2", START, 22.51, 114.21),
        gp("3", START, 22.52, 114.22),
        gp("4", START, 55.5, 77.7),  # hidden cell
    ]
    cells = grid.aggregate_cells(DAY, points)
    raw = grid.render_cells_csv(DAY, cells).decode()
    assert "413" not in raw and "mmsi" not in raw.lower()
    assert "55.5" not in raw and "77.7" not in raw
    rows = list(csv.DictReader(io.StringIO(raw)))
    assert rows == [
        {
            "utc_date": DAY,
            "cell_lat_min": "22.5",
            "cell_lat_max": "22.6",
            "cell_lon_min": "114.2",
            "cell_lon_max": "114.3",
            "distinct_vessels": "3",
        }
    ]


def test_empty_day_renders_header_only_csv():
    raw = grid.render_cells_csv(DAY, []).decode().strip().splitlines()
    assert raw == [",".join(grid.CSV_HEADER)]


@pytest.mark.parametrize("bad", ["", "20260101", "2026-1-1", "2026-13-01", "2026-02-30", "abc"])
def test_invalid_dates_rejected(bad):
    with pytest.raises(grid.DateError):
        grid.parse_utc_day(bad)


def test_valid_dates_accepted():
    assert grid.parse_utc_day("2026-01-01") == "2026-01-01"
