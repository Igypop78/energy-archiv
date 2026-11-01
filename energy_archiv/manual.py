"""Ručné hodnoty – napr. mesačná spotreba vody a plynu z faktúr.

CSV (oddeľovač ; alebo ,), hlavička voliteľná:

    velicina;obdobie;hodnota
    gas;2025-03;85.2              # mesačný súčet → rozdelí sa na hodiny bez iných dát
    water;2025-01-01..2025-03-31;12.5   # ľubovoľné obdobie (vrátane oboch dní)
    grid_import;2025-10-26 14:00;1.25   # jedna hodina → zdroj „oprava“ (najvyššia priorita)

Mesačné/obdobné hodnoty sa uložia ako zdroj „manual“ (najnižšia priorita) a doplnia
len hodiny, kde žiadny iný zdroj nemá údaj: (súčet − známe) / počet chýbajúcich hodín.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

from . import db


def _period(s: str, tz) -> tuple[int, int, bool]:
    """→ (začiatok, koniec, je_hodina) v UTC sekundách."""
    s = s.strip()
    if ".." in s:
        a, b = (date.fromisoformat(x.strip()) for x in s.split(".."))
        b += timedelta(days=1)
    elif len(s) == 7:
        y, m = map(int, s.split("-"))
        a = date(y, m, 1)
        b = date(y + (m == 12), m % 12 + 1, 1)
    elif len(s) == 10:
        a = date.fromisoformat(s)
        b = a + timedelta(days=1)
    else:
        t = datetime.fromisoformat(s).replace(minute=0, second=0, tzinfo=tz)
        u = int(t.timestamp())
        return u, u + 3600, True
    st = int(datetime(a.year, a.month, a.day, tzinfo=tz).timestamp())
    en = int(datetime(b.year, b.month, b.day, tzinfo=tz).timestamp())
    return st, en, False


def import_text(cfg, con, text: str) -> list[str]:
    tz = cfg.tz
    msgs = []
    sample = text[:500]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    for row in csv.reader(io.StringIO(text), delimiter=delim):
        if len(row) < 3 or row[0].strip().startswith("#"):
            continue
        key, per, val = row[0].strip(), row[1].strip(), row[2].strip().replace(",", ".")
        try:
            value = float(val)
            start, end, is_hour = _period(per, tz)
        except ValueError:
            continue  # hlavička alebo neplatný riadok
        db.ensure_series(con, key)
        if is_hour:
            db.upsert_hourly(con, key, "oprava", [(start, value)])
            msgs.append(f"{key} {per}: oprava {value}")
            continue
        prio = [p for p in cfg.priority(key) if p != "manual"]
        known = db.best_hourly(con, key, prio, start, end)
        db.delete_hourly(con, key, "manual", start, end)
        missing = [h for h in range(start, end, 3600) if h not in known]
        rest = value - sum(v for v, _ in known.values())
        if not missing:
            msgs.append(f"{key} {per}: všetky hodiny už majú údaje, nič sa nedopĺňa")
            continue
        if rest < 0:
            msgs.append(f"{key} {per}: známe hodiny už dávajú viac ({value - rest:.2f}) ako {value} – preskočené")
            continue
        per_hour = rest / len(missing)
        db.upsert_hourly(con, key, "manual", [(h, per_hour) for h in missing])
        msgs.append(f"{key} {per}: doplnených {len(missing)} hodín po {per_hour:.4f} (spolu {rest:.2f})")
    con.commit()
    db.log(con, "import ručné", "; ".join(msgs))
    return msgs
