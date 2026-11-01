"""Obnova histórie z archívu do štatistík Home Assistanta (recorder/import_statistics).

Kumulatívne súčty sa nadviažu na dáta, ktoré už v HA sú:
* ak HA má hodiny PO obnovovanom období → súčty sa dopočítajú spätne od nich
  (novšie dáta v HA zostanú nedotknuté, Energy panel nebude mať skok),
* inak sa pokračuje od posledného súčtu PRED obdobím (alebo od 0).
"""
from __future__ import annotations

import time

from . import db
from .ha import HA, HAError, _ts, iso
from .sync import FACTORS, TARGET_UNITS

HOUR = 3600


def _ha_sums(ha: HA, sid: str, start: int, end: int, units: dict | None = None,
             first_only: bool = False) -> list[dict]:
    rows = []
    t = start
    while t < end:
        t2 = min(t + 90 * 24 * HOUR, end)
        res = ha.call("recorder/statistics_during_period", start_time=iso(t), end_time=iso(t2),
                      statistic_ids=[sid], period="hour", types=["sum", "change"], units=units or TARGET_UNITS)
        for r in res.get(sid) or []:
            r["start"] = _ts(r["start"])
            rows.append(r)
        if first_only and any(r.get("sum") is not None for r in rows):
            break
        t = t2
    return rows


def plan(cfg, con, ha: HA, series: str, target: str | None = None,
         start: int | None = None, end: int | None = None) -> dict:
    s = con.execute("SELECT * FROM series WHERE key=?", (series,)).fetchone()
    if not s:
        raise ValueError(f"neznáma veličina {series}")
    if not target:
        m = con.execute("SELECT statistic_id FROM entity_map WHERE series_key=? AND valid_to IS NULL",
                        (series,)).fetchone()
        if not m:
            raise ValueError("veličina nemá aktívnu entitu – zadaj cieľ (--target)")
        target = m["statistic_id"]
    values = db.best_hourly(con, series, cfg.priority(series), start, end)
    if not values:
        raise ValueError("v archíve nie sú pre zvolené obdobie žiadne hodiny")
    first, last = min(values), max(values)

    meta = {m["statistic_id"]: m for m in ha.list_statistic_ids()}.get(target)
    unit_class = (meta or {}).get("unit_class")
    stored_unit = (meta or {}).get("statistics_unit_of_measurement") or s["unit"]
    factor = 1.0
    if unit_class in FACTORS and stored_unit in FACTORS[unit_class]:
        factor = FACTORS[unit_class][stored_unit]  # archív (kWh/m³) = HA * factor

    hours = list(range(first, last + HOUR, HOUR))
    filled = [h for h in hours if h not in values]
    sources: dict[str, int] = {}
    for _, src in values.values():
        sources[src] = sources.get(src, 0) + 1

    result = {"series": series, "target": target, "kind": s["kind"], "unit": stored_unit,
              "first": first, "last": last, "hours": len(hours), "zero_filled": len(filled),
              "sources": sources, "meta": meta, "warnings": []}
    if meta is None:
        result["warnings"].append(f"{target} v HA zatiaľ nemá štatistiky – vytvorí sa nová")

    if s["kind"] == "mean":
        result["stats"] = [{"start": iso(h), "mean": v / factor, "min": v / factor, "max": v / factor}
                           for h, (v, _) in values.items()]
        result["total"] = None
        return result

    deltas = [values.get(h, (0.0, None))[0] / factor for h in hours]
    total = sum(deltas)
    units = {unit_class: stored_unit} if unit_class else None
    after = _ha_sums(ha, target, last + HOUR, int(time.time()) // HOUR * HOUR + HOUR, units,
                     first_only=True)
    after = [r for r in after if r.get("sum") is not None]
    if after:
        a = after[0]
        # skutočný prírastok prvej hodiny po obnove: z archívu, inak to, čo hlási HA
        known = db.best_hourly(con, series, cfg.priority(series), a["start"], a["start"] + HOUR)
        c_a = known[a["start"]][0] / factor if known else (a.get("change") or 0.0)
        end_sum = a["sum"] - c_a  # súčet na konci poslednej obnovovanej hodiny
        if after[0]["start"] != last + HOUR:
            result["warnings"].append("medzi obnovou a ďalšími dátami v HA je medzera – nadväzujem na prvé ďalšie")
        if not known and deltas and c_a > 10 * max(1e-9, max(deltas)):
            result["warnings"].append(
                f"prvá hodina v HA po obnove má veľký prírastok {c_a:.2f} (pravdepodobne doháňanie po výpadku) – "
                "zváž obnovu s dlhším koncom")
        start_sum = end_sum - total
        result["anchor"] = f"nadviazané na súčet v HA o {iso(a['start'])}"
    else:
        before = _ha_sums(ha, target, first - 30 * 24 * HOUR, first, units)
        before = [r for r in before if r.get("sum") is not None]
        start_sum = before[-1]["sum"] if before else 0.0
        result["anchor"] = "pokračuje od posledného súčtu pred obnovou" if before else "začína od 0"

    stats, acc = [], start_sum
    for h, d in zip(hours, deltas):
        acc += d
        stats.append({"start": iso(h), "sum": acc, "state": acc})
    result["stats"] = stats
    result["total"] = total * factor
    return result


def _metadata_variants(p: dict) -> list[dict]:
    meta = p["meta"] or {}
    is_mean = p["kind"] == "mean"
    base = {"statistic_id": p["target"], "source": meta.get("source") or "recorder",
            "name": meta.get("name"), "unit_of_measurement": p["unit"], "has_sum": not is_mean}
    full = dict(base, has_mean=is_mean, mean_type=1 if is_mean else 0)
    if meta.get("unit_class") is not None or "unit_class" in meta:
        full["unit_class"] = meta.get("unit_class")
    v = [full, dict(base, has_mean=is_mean, mean_type=1 if is_mean else 0), dict(base, has_mean=is_mean),
         dict(base, mean_type=1 if is_mean else 0)]
    out = []
    for m in v:
        if m not in out:
            out.append(m)
    return out


def apply(con, ha: HA, p: dict, chunk: int = 2000) -> int:
    stats = p["stats"]
    last_err = None
    for md in _metadata_variants(p):
        try:
            ha.import_statistics(md, stats[:1])
        except HAError as e:
            last_err = e
            continue
        for i in range(0, len(stats), chunk):
            ha.import_statistics(md, stats[i:i + chunk])
        db.log(con, "obnova", f"{p['series']} → {p['target']}: {len(stats)} hodín ({p.get('anchor')})")
        return len(stats)
    raise HAError(f"HA odmietol import: {last_err}")
