"""HTTP tests for daily grid publications: privacy, fixity, races, rollback."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbmod
from app import main as mainmod
from app import store as storemod
from app.store import Store

BASE = 1767225600  # 2026-01-01T00:00:00Z
DAY = "2026-01-01"
HEADER = "station,mmsi,utc,lat,lon\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(mainmod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(mainmod, "DB_PATH", tmp_path / "data" / "ais.sqlite3")
    mainmod.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with TestClient(mainmod.app) as c:
        c.tmp_path = tmp_path
        yield c


def upload(client, name, body: bytes | str):
    payload = body.encode() if isinstance(body, str) else body
    return client.post(
        "/upload",
        files={"file": (name, io.BytesIO(payload), "text/csv")},
        follow_redirects=False,
    )


def csv_text(*rows: str) -> str:
    return HEADER + "".join(r + "\n" for r in rows)


def seed_day(client):
    # Three vessels inside cell (22.5-22.6, 114.2-114.3); one lone vessel in
    # another cell that must be suppressed (< 3 distinct MMSIs).
    body = csv_text(
        f"SZ01,413000001,{BASE},22.50,114.20",
        f"SZ01,413000002,{BASE},22.55,114.25",
        f"SZ01,413000003,{BASE},22.59,114.29",
        f"SZ01,413000004,{BASE},23.00,115.00",
    )
    assert upload(client, "day1.csv", body).status_code == 303


def publish(client, day=DAY):
    return client.post("/grids/publish", data={"day": day}, follow_redirects=False)


def test_grid_page_and_csv_show_only_cells_over_threshold(client):
    seed_day(client)
    resp = publish(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/grids/{DAY}"

    page = client.get(f"/grids/{DAY}")
    assert page.status_code == 200
    assert "22.5" in page.text and "114.2" in page.text and ">3<" in page.text
    # The suppressed cell and every MMSI are absent from the page.
    assert "23.0" not in page.text and "115.0" not in page.text
    assert "41300000" not in page.text

    csv_resp = client.get(f"/grids/{DAY}/grid.csv")
    assert csv_resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
    assert len(rows) == 1
    assert rows[0]["distinct_vessels"] == "3"
    assert all("413" not in line for line in csv_resp.text.splitlines())
    assert csv_resp.headers["x-content-sha256"] == hashlib.sha256(csv_resp.content).hexdigest()

    # The hidden cell is absent from the database too: no count, no MMSI leak.
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM grid_cells").fetchone()["c"] == 1
        assert conn.execute(
            "SELECT MIN(distinct_vessels) m FROM grid_cells"
        ).fetchone()["m"] >= 3
        table_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(grid_cells)").fetchall()
        }
        assert "mmsi" not in table_cols
    finally:
        conn.close()


def test_unpublished_day_is_404_and_reveals_nothing(client):
    seed_day(client)
    assert client.get("/grids/2026-01-02").status_code == 404
    assert client.get("/grids/2026-01-02/grid.csv").status_code == 404


def test_invalid_date_is_rejected_without_side_effects(client):
    seed_day(client)
    resp = client.post("/grids/publish", data={"day": "2026-13-40"}, follow_redirects=False)
    assert resp.status_code == 400
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM grid_publications").fetchone()["c"] == 0
    finally:
        conn.close()


def test_republish_is_idempotent_and_keeps_first_frozen_archive(client):
    seed_day(client)
    first = publish(client)
    assert first.status_code == 303
    csv1 = client.get(f"/grids/{DAY}/grid.csv").content
    sha1 = hashlib.sha256(csv1).hexdigest()

    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        meta1 = conn.execute(
            "SELECT * FROM grid_publications WHERE day = ?", (DAY,)
        ).fetchone()
    finally:
        conn.close()

    second = publish(client)
    assert second.status_code == 303
    csv2 = client.get(f"/grids/{DAY}/grid.csv").content
    assert csv2 == csv1

    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        rows = conn.execute("SELECT * FROM grid_publications WHERE day = ?", (DAY,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["published_at"] == meta1["published_at"]
        assert rows[0]["csv_sha256"] == sha1
    finally:
        conn.close()

    grid_files = list((mainmod.DATA_DIR / "grids").glob("grid_*.csv"))
    assert [p.name for p in grid_files] == [f"grid_{DAY}.csv"]


def test_late_telegrams_do_not_change_frozen_archive(client):
    seed_day(client)
    publish(client)
    before = client.get(f"/grids/{DAY}/grid.csv").content

    # A late batch adds a fourth vessel to the already published cell.  A fresh
    # computation would now report 4, but the frozen archive must stay at 3.
    late = csv_text(f"HK02,413000009,{BASE + 30},22.51,114.21")
    assert upload(client, "late.csv", late).status_code == 303

    publish(client)  # must be a no-op reuse
    after = client.get(f"/grids/{DAY}/grid.csv").content
    assert after == before
    assert ",4\n" not in after.decode()

    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        cell = conn.execute(
            "SELECT distinct_vessels FROM grid_cells WHERE day = ?", (DAY,)
        ).fetchone()
        assert cell["distinct_vessels"] == 3
        # The new batch itself was still committed to the track store.
        assert conn.execute(
            "SELECT COUNT(*) c FROM batches WHERE filename = 'late.csv'"
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_two_instances_publishing_same_day_produce_one_result(client, tmp_path):
    """Two Store instances (separate connections, same SQLite file/dir),
    the in-process analogue of two service containers on a shared volume."""

    seed_day(client)
    outcomes: list = []
    errors: list = []

    def worker():
        try:
            conn = dbmod.connect(mainmod.DB_PATH)
            try:
                outcomes.append(Store(mainmod.DATA_DIR, conn).publish_grid(DAY))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    assert len(outcomes) == 2
    assert {o.day for o in outcomes} == {DAY}
    assert sorted(o.reused for o in outcomes) == [False, True]
    assert {o.csv_sha256 for o in outcomes} == {outcomes[0].csv_sha256}

    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM grid_publications WHERE day = ?", (DAY,)
        ).fetchone()["c"] == 1
    finally:
        conn.close()

    files = list((mainmod.DATA_DIR / "grids").iterdir())
    assert [p.name for p in files] == [f"grid_{DAY}.csv"]
    # Both instances can serve the single complete CSV over HTTP.
    assert client.get(f"/grids/{DAY}/grid.csv").status_code == 200


def test_concurrent_publish_requests_leave_one_complete_archive(client):
    seed_day(client)
    statuses: list = []

    def worker():
        statuses.append(publish(client).status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses == [303] * 6

    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM grid_publications").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM grid_cells").fetchone()["c"] == 1
    finally:
        conn.close()


def test_concurrent_upload_and_publish_read_one_committed_state(client):
    seed_day(client)
    expected_sha: set[str] = set()

    # Compute what each of the two possible committed states would produce.
    def sha_for_state():
        conn = dbmod.connect(mainmod.DB_PATH)
        try:
            with dbmod.read_tx(conn):
                rows = conn.execute(
                    "SELECT mmsi, utc, lat, lon FROM track_points"
                ).fetchall()
        finally:
            conn.close()
        from app import grid

        cells = grid.aggregate_cells(
            DAY,
            (grid.GridPoint(r["mmsi"], r["utc"], r["lat"], r["lon"]) for r in rows),
        )
        return hashlib.sha256(grid.render_cells_csv(DAY, cells)).hexdigest()

    expected_sha.add(sha_for_state())  # pre-upload state

    barrier = threading.Barrier(2)

    def do_upload():
        barrier.wait()
        upload(
            client,
            "racer.csv",
            csv_text(f"HK02,413000007,{BASE},22.52,114.22"),
        )

    def do_publish():
        barrier.wait()
        publish(client)

    t1 = threading.Thread(target=do_upload)
    t2 = threading.Thread(target=do_publish)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    expected_sha.add(sha_for_state())  # post-upload state
    served = client.get(f"/grids/{DAY}/grid.csv")
    assert served.status_code == 200
    assert hashlib.sha256(served.content).hexdigest() in expected_sha


def test_disk_write_failure_leaves_no_residue_and_retry_succeeds(client, monkeypatch):
    seed_day(client)
    # Manual save/restore rather than monkeypatch.undo(): the client fixture
    # itself monkeypatches DATA_DIR/DB_PATH, and undo() would revert those too.
    real_rename = os.rename

    def failing_rename(src, dst, *args, **kwargs):
        if str(dst).endswith(f"grid_{DAY}.csv"):
            raise OSError("simulated disk failure")
        return real_rename(src, dst, *args, **kwargs)

    storemod.os.rename = failing_rename
    resp = publish(client)
    assert resp.status_code == 503
    assert "未生成可下载文件" in resp.text

    grids_dir = mainmod.DATA_DIR / "grids"
    assert not list(grids_dir.glob("grid_*.csv"))
    assert not list(grids_dir.glob(".staging-*"))
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM grid_publications").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM grid_cells").fetchone()["c"] == 0
        # Existing tracks and originals are untouched.
        assert conn.execute("SELECT COUNT(*) c FROM track_points").fetchone()["c"] == 4
        assert conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"] == 1
    finally:
        conn.close()
    assert client.get(f"/grids/{DAY}").status_code == 404

    storemod.os.rename = real_rename
    assert publish(client).status_code == 303
    assert client.get(f"/grids/{DAY}/grid.csv").status_code == 200


def test_registry_failure_after_write_removes_file_and_rolls_back(client):
    seed_day(client)
    # The CSV write succeeds, then the registry insert fails inside the open
    # transaction.  The sqlite3.Connection type and its instance attributes are
    # read-only, so wrap connections from the factory in a delegating proxy.
    real_connect = mainmod.connect

    class ConnProxy:
        def __init__(self, inner):
            object.__setattr__(self, "_inner", inner)

        def executemany(self, sql, parameters):
            if "INSERT INTO grid_cells" in sql:
                raise sqlite3.OperationalError("simulated registry failure")
            return self._inner.executemany(sql, parameters)

        def __getattr__(self, item):
            return getattr(object.__getattribute__(self, "_inner"), item)

        def __setattr__(self, key, value):
            setattr(object.__getattribute__(self, "_inner"), key, value)

    # Manual save/restore: monkeypatch.undo() would also revert the client
    # fixture's own DATA_DIR/DB_PATH patches, breaking the retry.
    mainmod.connect = lambda db_path: ConnProxy(real_connect(db_path))
    try:
        resp = publish(client)
        assert resp.status_code == 503

        assert not list((mainmod.DATA_DIR / "grids").glob("grid_*.csv"))
        assert not list((mainmod.DATA_DIR / "grids").glob(".staging-*"))
        conn = dbmod.connect(mainmod.DB_PATH)
        try:
            assert conn.execute("SELECT COUNT(*) c FROM grid_publications").fetchone()["c"] == 0
            assert conn.execute("SELECT COUNT(*) c FROM grid_cells").fetchone()["c"] == 0
            # The track store remains intact.
            assert conn.execute("SELECT COUNT(*) c FROM track_points").fetchone()["c"] == 4
            assert conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"] == 1
        finally:
            conn.close()
    finally:
        mainmod.connect = real_connect

    assert publish(client).status_code == 303
    assert client.get(f"/grids/{DAY}/grid.csv").status_code == 200


def test_day_without_enough_vessels_publishes_header_only_csv(client):
    body = csv_text(f"SZ01,413000001,{BASE},22.5,114.2")
    upload(client, "solo.csv", body)
    assert publish(client, DAY).status_code == 303
    page = client.get(f"/grids/{DAY}")
    assert page.status_code == 200
    csv_resp = client.get(f"/grids/{DAY}/grid.csv")
    lines = csv_resp.text.strip().splitlines()
    assert len(lines) == 1  # header only, no hidden counts
    assert "413" not in csv_resp.text


def test_restart_cleans_orphan_grid_files_and_keeps_frozen_ones(client, tmp_path):
    seed_day(client)
    publish(client)
    grids_dir = mainmod.DATA_DIR / "grids"
    (grids_dir / ".staging-leftover.tmp").write_text("partial")
    (grids_dir / "grid_1999-01-01.csv").write_text("unreferenced")

    with TestClient(mainmod.app):
        assert not (grids_dir / ".staging-leftover.tmp").exists()
        assert not (grids_dir / "grid_1999-01-01.csv").exists()
        assert (grids_dir / f"grid_{DAY}.csv").exists()
    assert client.get(f"/grids/{DAY}/grid.csv").status_code == 200


def test_grids_index_lists_publications(client):
    seed_day(client)
    page = client.get("/grids")
    assert page.status_code == 200
    assert DAY not in page.text
    publish(client)
    page = client.get("/grids")
    assert DAY in page.text and "grid.csv" in page.text
