"""Dopočítanie nákladov a kompenzácie podľa tarify: hodinová energia × cena platná v tej hodine.

Výsledok sa uloží ako zdroj „vypocet“ do veličín *_cost / *_compensation. Hodnoty z HA
majú prednosť (priorita), výpočet teda dopĺňa len obdobie, keď HA náklady nepočítal.

Konfigurácia v config.yaml, napr.:

naklady:
  grid_import_cost:
    energia: grid_import
    tarify:
      - od: 2024-01-01
        typ: vt_nt
        nt: ["23:45-05:45", "13:45-15:45"]
        nt_cena: 0.12
        vt_cena: 0.21
  grid_export_compensation:
    energia: grid_export
    tarify:
      - od: 2024-01-01
        typ: pasma
        pasma: ["00:00-10:00=0.08211", "10:00-18:00=0.04284", "18:00-24:00=0.08092"]
        vikend_sviatok: 0.04046
      - od: 2026-09-01
        typ: cena_z_veliciny
        velicina: grid_export_price
        delitel: 1000          # €/MWh → €/kWh
  water_cost:
    energia: water
    tarify:
      - {od: 2024-01-01, typ: pevna, cena: 3.0574}
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import db

HOUR = 3600


def easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def sk_holidays(y: int) -> set[date]:
    """Dni pracovného pokoja na Slovensku (zákon 241/1993 v znení konsolidácie)."""
    days = {(1, 1), (1, 6), (5, 1), (7, 5), (8, 29), (11, 1), (12, 24), (12, 25), (12, 26)}
    if y < 2024:
        days.add((9, 1))
    if y != 2026:          # v roku 2026 dočasne pracovné dni
        days |= {(5, 8), (9, 15)}
    if y < 2025:           # od 2025 17. 11. nie je dňom pracovného pokoja
        days.add((11, 17))
    out = {date(y, m, d) for m, d in days}
    e = easter(y)
    out |= {e - timedelta(days=2), e + timedelta(days=1)}
    return out


def _mins(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def _intervals(spec) -> list[tuple[int, int]]:
    """"23:45-05:45" → [(1425, 1440), (0, 345)]"""
    out = []
    for item in spec:
        a, b = (_mins(x) for x in str(item).split("=")[0].split("-"))
        if b == 0:
            b = 1440
        out += [(a, 1440), (0, b)] if b <= a else [(a, b)]
    return out


def _overlap(h0: int, ivs) -> float:
    """Podiel hodiny (začiatok v minútach dňa) pokrytý intervalmi."""
    return sum(max(0, min(h0 + 60, b) - max(h0, a)) for a, b in ivs) / 60


def price_fn(t: dict, con, cfg):
    typ = t["typ"]
    if typ == "pevna":
        c = float(t["cena"])
        return lambda ts, loc: c
    if typ == "vt_nt":
        ivs = _intervals(t["nt"])
        nt, vt = float(t["nt_cena"]), float(t["vt_cena"])

        def f(ts, loc):
            share = _overlap(loc.hour * 60, ivs)
            return share * nt + (1 - share) * vt
        return f
    if typ == "pasma":
        bands = [(_intervals([b.split("=")[0]]), float(b.split("=")[1])) for b in t["pasma"]]
        wk = t.get("vikend_sviatok")
        hol_cache: dict[int, set] = {}

        def f(ts, loc):
            if wk is not None:
                hs = hol_cache.setdefault(loc.year, sk_holidays(loc.year))
                if loc.weekday() >= 5 or loc.date() in hs:
                    return float(wk)
            return sum(_overlap(loc.hour * 60, ivs) * p for ivs, p in bands)
        return f
    if typ == "cena_z_veliciny":
        series = t["velicina"]
        div = float(t.get("delitel", 1))
        prices = {ts: v for ts, (v, _) in db.best_hourly(con, series, cfg.priority(series)).items()}
        return lambda ts, loc: (prices[ts] / div) if ts in prices else None
    raise ValueError(f"neznámy typ tarify {typ}")


def compute(cfg, con, start: int | None = None, end: int | None = None, only: str | None = None) -> list[str]:
    tz = cfg.tz
    msgs = []
    for key, spec in (cfg.data.get("naklady") or {}).items():
        if only and key != only:
            continue
        energy = spec["energia"]
        tarify = sorted(spec["tarify"], key=lambda t: str(t["od"]))
        fns = [(int(datetime.fromisoformat(str(t["od"])).replace(tzinfo=tz).timestamp()), price_fn(t, con, cfg))
               for t in tarify]
        vals = db.best_hourly(con, energy, cfg.priority(energy), start, end)
        rows, missing_price = [], 0
        for ts, (kwh, _src) in vals.items():
            fn = None
            for since, f in fns:
                if ts >= since:
                    fn = f
            if fn is None:
                continue
            p = fn(ts, datetime.fromtimestamp(ts, tz))
            if p is None:
                missing_price += 1
                continue
            rows.append((ts, kwh * p))
        db.ensure_series(con, key, "sum", "EUR")
        lo = start if start is not None else (min(vals) if vals else 0)
        hi = end if end is not None else (max(vals) + HOUR if vals else 0)
        db.delete_hourly(con, key, "vypocet", lo, hi)
        db.upsert_hourly(con, key, "vypocet", rows)
        con.commit()
        per_year: dict[int, float] = {}
        for ts, v in rows:
            y = datetime.fromtimestamp(ts, tz).year
            per_year[y] = per_year.get(y, 0) + v
        yrs = ", ".join(f"{y}: {v:.2f} €" for y, v in sorted(per_year.items()))
        msg = f"{key}: {len(rows)} hodín z {energy} ({yrs})"
        if missing_price:
            msg += f"; bez ceny {missing_price} h"
        msgs.append(msg)
    db.log(con, "náklady", "; ".join(msgs))
    return msgs
