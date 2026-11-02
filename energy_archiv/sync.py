"""Zber hodinových štatistík z HA a automatické mapovanie entít Energy panela."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db
from .ha import HA

HOUR = 3600
CHUNK = 60 * 24 * HOUR  # 60 dní na jeden dotaz
TARGET_UNITS = {"energy": "kWh", "volume": "m³"}

# prevod do jednotky archívu (kWh / m³)
FACTORS = {
    "energy": {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0, "GWh": 1e6, "MJ": 1 / 3.6, "GJ": 1000 / 3.6},
    "volume": {"L": 0.001, "m³": 1.0, "ft³": 0.0283168466, "gal": 0.00378541178, "CCF": 2.83168466},
}

ROLE_LABELS = {
    "grid_import": "Sieť – odber", "grid_export": "Sieť – dodávka", "solar": "FVE – výroba",
    "battery_in": "Batéria – nabíjanie", "battery_out": "Batéria – vybíjanie",
    "gas": "Plyn", "water": "Voda", "device": "Spotrebič", "water_device": "Voda – spotrebič",
}


@dataclass
class Slot:
    series: str
    role: str
    statistic_id: str
    kind: str = "sum"
    label: str | None = None


def _slug(sid: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", sid.split(".", 1)[-1].split(":", 1)[-1].lower()).strip("_")


def discover(prefs: dict, known_ids: set[str], cost_sensors: dict | None = None) -> list[Slot]:
    """Z nastavení Energy panela zostaví zoznam veličín (vrátane nákladov a cien)."""
    slots: list[Slot] = []
    counters: dict[str, int] = {}

    def key_for(role: str) -> str:
        n = counters.get(role, 0)
        counters[role] = n + 1
        return role if n == 0 else f"{role}_{n + 1}"

    def add(role, sid, cost=None, price=None, cost_suffix="cost"):
        if not sid:
            return
        key = key_for(role)
        slots.append(Slot(key, role, sid, "sum", ROLE_LABELS.get(role, role)))
        auto = (cost_sensors or {}).get(sid)   # HA: energy/info → cost_sensors (automatické entity nákladov)
        cost_id = cost or (auto if auto and auto in known_ids else None) or (f"{sid}_{cost_suffix}" if f"{sid}_{cost_suffix}" in known_ids else None)
        if cost_id:
            name = "náklady" if cost_suffix == "cost" else "kompenzácia"
            slots.append(Slot(f"{key}_{cost_suffix}", role, cost_id, "sum",
                              f"{ROLE_LABELS.get(role, role)} – {name}"))
        if price and price in known_ids:
            slots.append(Slot(f"{key}_price", role, price, "mean", f"{ROLE_LABELS.get(role, role)} – cena"))

    for src in prefs.get("energy_sources") or []:
        t = src.get("type")
        if t == "grid":
            flows_from = src.get("flow_from") or ([src] if src.get("stat_energy_from") else [])
            for f in flows_from:
                add("grid_import", f.get("stat_energy_from"), f.get("stat_cost"), f.get("entity_energy_price"))
            flows_to = src.get("flow_to") or ([src] if src.get("stat_energy_to") else [])
            for f in flows_to:
                add("grid_export", f.get("stat_energy_to"), f.get("stat_compensation"),
                    f.get("entity_energy_price_export") or f.get("entity_energy_price"), "compensation")
        elif t == "solar":
            add("solar", src.get("stat_energy_from"))
        elif t == "battery":
            add("battery_out", src.get("stat_energy_from"))
            add("battery_in", src.get("stat_energy_to"))
        elif t in ("gas", "water"):
            add(t, src.get("stat_energy_from"), src.get("stat_cost"), src.get("entity_energy_price"))
    for dev in prefs.get("device_consumption") or []:
        sid = dev.get("stat_consumption")
        if sid:
            slots.append(Slot(f"device_{_slug(sid)}", "device", sid, "sum", dev.get("name")))
    for dev in prefs.get("device_consumption_water") or []:
        sid = dev.get("stat_consumption")
        if sid:
            slots.append(Slot(f"water_device_{_slug(sid)}", "water_device", sid, "sum", dev.get("name")))
    return slots


def _hour(ts: float) -> int:
    return int(ts) // HOUR * HOUR


def _parse_day(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def first_hour(ha: HA, sid: str, start: int, end: int, kind: str) -> int | None:
    """Prvá hodina, pre ktorú má HA štatistiku (hľadá po rokoch)."""
    t = start
    year = 365 * 24 * HOUR
    while t < end:
        rows = ha.hourly([sid], t, min(t + year, end), ["mean"] if kind == "mean" else ["sum"]).get(sid)
        if rows:
            return min(r["start"] for r in rows)
        t += year
    return None


def last_hour(ha: HA, sid: str, start: int, end: int, kind: str) -> int | None:
    """Posledná hodina so štatistikou (hľadá po rokoch odzadu)."""
    year = 365 * 24 * HOUR
    t = end
    while t > start:
        rows = ha.hourly([sid], max(t - year, start), t, ["mean"] if kind == "mean" else ["sum"]).get(sid)
        if rows:
            return max(r["start"] for r in rows)
        t -= year
    return None


def reconcile_maps(con, ha: HA, slots: list[Slot], meta: dict, hist_start: int, now: int) -> list[str]:
    """Priradí entity k veličinám; pri výmene entity nadviaže históriu."""
    notes = []
    for s in slots:
        m = meta.get(s.statistic_id, {})
        unit_class = m.get("unit_class")
        unit = TARGET_UNITS.get(unit_class) or m.get("statistics_unit_of_measurement")
        db.ensure_series(con, s.series, s.kind, unit, s.label or m.get("name"), s.role)
        active = con.execute("SELECT * FROM entity_map WHERE series_key=? AND valid_to IS NULL",
                             (s.series,)).fetchone()
        if active and active["statistic_id"] == s.statistic_id:
            continue
        if active:
            start = first_hour(ha, s.statistic_id, hist_start, now, s.kind)
            switch = start if start is not None and start > (active["valid_from"] or 0) else now
            con.execute("UPDATE entity_map SET valid_to=? WHERE id=?", (switch, active["id"]))
            con.execute(
                "INSERT INTO entity_map(series_key, statistic_id, valid_from, note, created) VALUES (?,?,?,?,?)",
                (s.series, s.statistic_id, switch, f"nahradila {active['statistic_id']}", int(time.time())))
            msg = (f"{s.series}: {active['statistic_id']} → {s.statistic_id} od "
                   f"{datetime.fromtimestamp(switch, timezone.utc):%Y-%m-%d %H:%M} UTC")
        else:
            con.execute("INSERT INTO entity_map(series_key, statistic_id, valid_from, created) VALUES (?,?,?,?)",
                        (s.series, s.statistic_id, None, int(time.time())))
            msg = f"{s.series}: nová veličina ← {s.statistic_id}"
        db.log(con, "mapovanie", msg)
        notes.append(msg)
    con.commit()
    return notes


def sync(cfg, con, ha: HA, now: int | None = None, full: bool = False) -> dict:
    now = _hour(now or time.time())
    hist_start = _parse_day(cfg["ha"]["history_start"])
    resync = int(cfg["ha"]["resync_hours"]) * HOUR

    prefs = ha.energy_prefs()
    try:
        cost_sensors = (ha.call("energy/info") or {}).get("cost_sensors") or {}
    except Exception:
        cost_sensors = {}
    meta = {m["statistic_id"]: m for m in ha.list_statistic_ids()}
    slots = discover(prefs, set(meta), cost_sensors)
    notes = reconcile_maps(con, ha, slots, meta, hist_start, now)

    stats = {"mapovanie": notes, "hodiny": 0, "veliciny": 0}
    maps = con.execute(
        "SELECT m.*, s.kind FROM entity_map m JOIN series s ON s.key=m.series_key "
        "WHERE m.valid_to IS NULL OR m.synced_until IS NULL OR m.synced_until < m.valid_to").fetchall()
    for m in maps:
        start = m["valid_from"] or hist_start
        if m["synced_until"] and not full:
            start = max(start, m["synced_until"] - resync)
        end = min(m["valid_to"] or now, now)
        sid, kind, total = m["statistic_id"], m["kind"], 0
        t = start
        while t < end:
            t2 = min(t + CHUNK, end)
            types = ["mean"] if kind == "mean" else ["change"]
            rows = ha.call("recorder/statistics_during_period",
                           start_time=_iso(t), end_time=_iso(t2), statistic_ids=[sid],
                           period="hour", types=types, units=TARGET_UNITS).get(sid) or []
            vals = []
            for r in rows:
                v = r.get("mean" if kind == "mean" else "change")
                if v is None:
                    continue
                vals.append((_ts(r["start"]), v))
            total += db.upsert_hourly(con, m["series_key"], "ha", vals)
            t = t2
        con.execute("UPDATE entity_map SET synced_until=? WHERE id=?", (end, m["id"]))
        con.commit()
        stats["hodiny"] += total
        stats["veliciny"] += 1
    db.log(con, "sync", f"{stats['veliciny']} veličín, {stats['hodiny']} hodín")
    return stats


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _ts(v) -> int:
    from .ha import _ts as conv
    return conv(v)
