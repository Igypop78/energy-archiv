"""SQLite archív.

Hodnoty sa ukladajú po hodinách (začiatok hodiny v UTC, unix sekundy) ako
**prírastok** za hodinu (kWh, m³, €) pri kumulatívnych veličinách, alebo ako
priemer pri cenách. Každý zdroj (ha, zsd, deye, manual, oprava) má vlastný
riadok – nič sa neprepisuje medzi zdrojmi, o použitej hodnote rozhoduje priorita.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    key      TEXT PRIMARY KEY,          -- stály názov veličiny, napr. grid_import
    kind     TEXT NOT NULL DEFAULT 'sum',  -- sum = prírastky, mean = priemer (cena)
    unit     TEXT,
    label    TEXT,
    role     TEXT                       -- odkiaľ z Energy panela pochádza
);
CREATE TABLE IF NOT EXISTS entity_map (
    id            INTEGER PRIMARY KEY,
    series_key    TEXT NOT NULL REFERENCES series(key),
    statistic_id  TEXT NOT NULL,
    valid_from    INTEGER,              -- vrátane; NULL = od začiatku
    valid_to      INTEGER,              -- bez; NULL = doteraz
    synced_until  INTEGER,
    note          TEXT,
    created       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS entity_map_series ON entity_map(series_key);
CREATE TABLE IF NOT EXISTS hourly (
    series_key TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    source     TEXT NOT NULL,
    value      REAL NOT NULL,
    PRIMARY KEY (series_key, ts, source)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily_ref (       -- denné súčty zo zdrojov (Deye) na kontrolu
    series_key TEXT NOT NULL,
    day        TEXT NOT NULL,
    source     TEXT NOT NULL,
    value      REAL NOT NULL,
    PRIMARY KEY (series_key, day, source)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS registers (       -- odpočty elektromera (ZSD)
    series_key TEXT NOT NULL,
    day        TEXT NOT NULL,
    source     TEXT NOT NULL,
    value      REAL NOT NULL,
    kind       TEXT,
    PRIMARY KEY (series_key, day, source)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS log (
    id     INTEGER PRIMARY KEY,
    ts     INTEGER NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def log(con, action: str, detail: str = "") -> None:
    con.execute("INSERT INTO log(ts, action, detail) VALUES (?,?,?)", (int(time.time()), action, detail))
    con.commit()


def ensure_series(con, key: str, kind: str = "sum", unit: str | None = None,
                  label: str | None = None, role: str | None = None) -> None:
    con.execute(
        """INSERT INTO series(key, kind, unit, label, role) VALUES (?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET
             unit  = COALESCE(excluded.unit, series.unit),
             label = COALESCE(excluded.label, series.label),
             role  = COALESCE(excluded.role, series.role)""",
        (key, kind, unit, label, role),
    )


def upsert_hourly(con, series: str, source: str, rows) -> int:
    rows = [(series, int(ts), source, float(v)) for ts, v in rows]
    con.executemany(
        "INSERT INTO hourly(series_key, ts, source, value) VALUES (?,?,?,?) "
        "ON CONFLICT(series_key, ts, source) DO UPDATE SET value = excluded.value",
        rows,
    )
    return len(rows)


def delete_hourly(con, series: str, source: str, start: int, end: int) -> int:
    cur = con.execute("DELETE FROM hourly WHERE series_key=? AND source=? AND ts>=? AND ts<?",
                      (series, source, start, end))
    return cur.rowcount


def best_hourly(con, series: str, priority: list[str], start: int | None = None,
                end: int | None = None) -> dict[int, tuple[float, str]]:
    """Pre každú hodinu vráti (hodnota, zdroj) podľa priority zdrojov."""
    q = "SELECT ts, source, value FROM hourly WHERE series_key=?"
    args: list = [series]
    if start is not None:
        q += " AND ts>=?"; args.append(start)
    if end is not None:
        q += " AND ts<?"; args.append(end)
    rank = {s: i for i, s in enumerate(priority)}
    out: dict[int, tuple[float, str, int]] = {}
    for ts, src, val in con.execute(q, args):
        r = rank.get(src, len(rank))
        if ts not in out or r < out[ts][2]:
            out[ts] = (val, src, r)
    return {ts: (v, s) for ts, (v, s, _) in sorted(out.items())}


def series_list(con) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM series ORDER BY role, key").fetchall()


def active_maps(con) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM entity_map WHERE valid_to IS NULL ORDER BY series_key").fetchall()


def all_maps(con) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM entity_map ORDER BY series_key, COALESCE(valid_from,0)").fetchall()
