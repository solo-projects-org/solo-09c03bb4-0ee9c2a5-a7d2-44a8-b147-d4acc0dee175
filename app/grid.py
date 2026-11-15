"""Daily 0.1-degree grid density over *trusted* AIS points.

The port planning group receives vessel *activity density*, never individual
tracks.  Every output cell therefore aggregates distinct MMSIs only: the same
vessel reporting many times inside one cell on one UTC day is counted once,
and no MMSI ever leaves this module.

Privacy rule (k-anonymity, k=3): a cell with fewer than ``MIN_VESSELS_PER_CELL``
distinct vessels is suppressed.  Suppressed cells are never written to a page,
a CSV or the database, so neither their count nor any vessel identifier can
leak.  The rule is applied inside :func:`aggregate_cells`, before persistence,
and every reader below consumes its already-filtered output.

The grid is fixed at :data:`GRID_STEP_DEGREES` = 0.1 degree.  Cell indices are
integers so the boundaries are exact (no ``math.floor`` float drift): a point
at longitude 114.29 belongs to cell 1142, one at 114.30 belongs to cell 1143,
and negative coordinates floor correctly (-0.01 -> index -1, i.e. -0.1..0.0).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

GRID_STEP_DEGREES = 0.1
# Microdegrees per grid step; positions are stored rounded to 6 decimals.
_MICRO = 1_000_000
_STEP_MICRO = int(GRID_STEP_DEGREES * _MICRO)  # 100_000

MIN_VESSELS_PER_CELL = 3

CSV_HEADER = [
    "utc_date",
    "cell_lat_min",
    "cell_lat_max",
    "cell_lon_min",
    "cell_lon_max",
    "distinct_vessels",
]

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class DateError(ValueError):
    """The requested UTC day is not a valid ``YYYY-MM-DD`` date."""


# --------------------------------------------------------------------------
# Day helpers
# --------------------------------------------------------------------------


def parse_utc_day(text: str) -> str:
    match = _DATE_RE.match(text or "")
    if not match:
        raise DateError("日期必须是 YYYY-MM-DD 格式的 UTC 日")
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise DateError(f"不是有效日历日期: {text}") from exc
    return text


def utc_day_bounds(day: str) -> tuple[int, int]:
    """Return the half-open ``[start, end)`` Unix-second range of a UTC day."""

    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def utc_day_of_timestamp(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Fixed grid
# --------------------------------------------------------------------------


def cell_index(value: float) -> int:
    """0.1-degree cell index for one coordinate, flooring toward -infinity."""

    micro = int(round(value * _MICRO))
    return micro // _STEP_MICRO


def cell_bounds(index: int) -> tuple[float, float]:
    lo = index * _STEP_MICRO / _MICRO
    hi = (index + 1) * _STEP_MICRO / _MICRO
    return lo, hi


@dataclass(frozen=True)
class GridPoint:
    """One trusted point participating in a daily aggregation."""

    mmsi: str
    utc: int
    lat: float
    lon: float


@dataclass(frozen=True)
class GridCell:
    day: str
    lat_index: int
    lon_index: int
    distinct_vessels: int

    @property
    def lat_min(self) -> float:
        return cell_bounds(self.lat_index)[0]

    @property
    def lat_max(self) -> float:
        return cell_bounds(self.lat_index)[1]

    @property
    def lon_min(self) -> float:
        return cell_bounds(self.lon_index)[0]

    @property
    def lon_max(self) -> float:
        return cell_bounds(self.lon_index)[1]


def aggregate_cells(day: str, points: Iterable[GridPoint]) -> list[GridCell]:
    """Count distinct MMSIs per fixed cell; suppress sub-threshold cells.

    The ``(cell, mmsi)`` set collapses repeated reports from one vessel inside
    a cell to a single count.  Cells below :data:`MIN_VESSELS_PER_CELL` are
    dropped here and are unknown to every downstream consumer.
    """

    start, end = utc_day_bounds(day)
    seen: set[tuple[int, int, str]] = set()
    for point in points:
        if point.utc < start or point.utc >= end:
            continue
        lat_idx = cell_index(point.lat)
        lon_idx = cell_index(point.lon)
        seen.add((lat_idx, lon_idx, point.mmsi))

    counts: dict[tuple[int, int], int] = {}
    for lat_idx, lon_idx, _mmsi in seen:
        counts[(lat_idx, lon_idx)] = counts.get((lat_idx, lon_idx), 0) + 1

    cells = [
        GridCell(day=day, lat_index=lat_idx, lon_index=lon_idx, distinct_vessels=n)
        for (lat_idx, lon_idx), n in counts.items()
        if n >= MIN_VESSELS_PER_CELL
    ]
    cells.sort(key=lambda c: (c.lat_index, c.lon_index))
    return cells


def render_cells_csv(day: str, cells: Sequence[GridCell]) -> bytes:
    """Serialise publishable cells.  Contains no MMSI and no hidden cells."""

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for cell in cells:
        writer.writerow(
            [
                day,
                f"{cell.lat_min:.1f}",
                f"{cell.lat_max:.1f}",
                f"{cell.lon_min:.1f}",
                f"{cell.lon_max:.1f}",
                cell.distinct_vessels,
            ]
        )
    return buf.getvalue().encode("utf-8")
