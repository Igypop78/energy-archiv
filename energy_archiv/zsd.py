"""Import exportov z portálu ZSD (Západoslovenská distribučná).

Podporované:
* „Profilové dáta – namerané hodnoty“ (15 min, kW) → hodinové kWh
* „Registrové dáta – Odpočty na prístroji“ → tabuľka registers
* list „nameraná hodinová práca“ sa preskakuje (pri prechode na zimný čas stráca hodinu)

Súbory: .xls (xlrd), .xlsx (openpyxl) alebo .csv so stĺpcami oddelenými bodkočiarkou.
Pri prechode na zimný čas ZSD označuje opakovanú hodinu sekundou: 02:00:01, 02:15:01 …
"""
from __future__ import annotations

import csv
import io
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return " ".join(s.split())


def read_sheets(path: str | Path, data: bytes | None = None) -> dict[str, list[list]]:
    """Vráti {názov listu: riadky}. Hodnoty sú str alebo float."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".xls":
        import xlrd
        wb = xlrd.open_workbook(file_contents=data) if data else xlrd.open_workbook(str(path))
        out = {}
        for sh in wb.sheets():
            rows = []
            for r in range(sh.nrows):
                row = []
                for c in range(sh.ncols):
                    cell = sh.cell(r, c)
                    v = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        v = xlrd.xldate.xldate_as_datetime(v, wb.datemode)
                    row.append(v)
                rows.append(row)
            out[sh.name] = rows
        return out
    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data) if data else path, read_only=True, data_only=True)
        return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
    text = (data.decode("utf-8-sig") if data else path.read_text(encoding="utf-8-sig"))
    return {path.stem: [row for row in csv.reader(io.StringIO(text), delimiter=";")]}


def _cells(row) -> list:
    return [("" if c is None else c) for c in row]


def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _time(v) -> tuple[int, int, int] | None:
    if isinstance(v, datetime):
        return v.hour, v.minute, v.second
    if isinstance(v, float) and 0 <= v < 1:  # zlomok dňa z Excelu
        secs = round(v * 86400)
        return secs // 3600, secs // 60 % 60, secs % 60
    parts = str(v).strip().split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
        s = int(float(parts[2])) if len(parts) > 2 else 0
    except ValueError:
        return None
    return h, m, s


def _header_info(rows) -> dict:
    info = {}
    for row in rows[:40]:
        cells = [str(c).strip() for c in _cells(row) if str(c).strip()]
        if len(cells) >= 2:
            k = _norm(cells[0]).rstrip(":")
            info[k] = cells[1]
        elif len(cells) == 1:
            info.setdefault("_titles", []).append(cells[0])
    return info


def parse_profile(rows, tz: ZoneInfo) -> dict:
    """15-min profil → {'type', 'eic', 'unit', 'hours': {utc_ts: kWh}, 'intervals': n}."""
    info = _header_info(rows)
    unit = _norm(info.get("jednotka", "kw"))
    ptype = info.get("typ profilu", "")
    hours: dict[int, float] = defaultdict(float)
    n = 0
    in_data = False
    for row in rows:
        cells = _cells(row)
        normed = [_norm(c) for c in cells]
        if "datum" in normed and any(x.startswith("cas") for x in normed):
            idx_d = normed.index("datum")
            idx_t = next(i for i, x in enumerate(normed) if x.startswith("cas"))
            idx_v = idx_t + 1
            in_data = True
            continue
        if not in_data:
            continue
        d = _date(cells[idx_d]) if len(cells) > idx_d else None
        t = _time(cells[idx_t]) if len(cells) > idx_t else None
        v = _num(cells[idx_v]) if len(cells) > idx_v else None
        if d is None or t is None or v is None:
            continue
        h, m, s = t
        local = datetime(d.year, d.month, d.day, h, m, tzinfo=tz, fold=1 if s == 1 else 0)
        utc = local.astimezone(timezone.utc)
        kwh = v / 4 if unit in ("kw", "kvar") else v  # kW priemer za 15 min → kWh
        hours[int(utc.timestamp()) // 3600 * 3600] += kwh
        n += 1
    return {"type": ptype, "eic": info.get("eic"), "unit": unit, "hours": dict(hours), "intervals": n}


def parse_registers(rows) -> list[dict]:
    out, cols = [], None
    for row in rows:
        cells = _cells(row)
        normed = [_norm(c) for c in cells]
        if "datum odpoctu" in normed:
            cols = {k: normed.index(k) for k in normed if k}
            continue
        if not cols:
            continue
        d = _date(cells[cols["datum odpoctu"]])
        v = _num(cells[cols.get("odpocitany stav", -1)]) if "odpocitany stav" in cols else None
        if d is None or v is None:
            continue
        ident = str(cells[cols.get("ident. pocitadla", 1)]).strip()
        out.append({"day": d.isoformat(), "value": v, "ident": ident,
                    "kind": str(cells[cols["druh odpoctu"]]).strip() if "druh odpoctu" in cols else None})
    return out


def series_for(text: str, mapping: dict) -> str:
    t = _norm(text)
    return mapping["dodavka"] if "dodav" in t else mapping["odber"]


def import_file(cfg, con, path: str | Path, data: bytes | None = None, series: str | None = None) -> list[str]:
    """Naimportuje súbor ZSD; vráti zoznam správ."""
    tz = cfg.tz
    msgs = []
    sheets = read_sheets(path, data)
    mapping = cfg["zsd"]["map"]
    for name, rows in sheets.items():
        title = " ".join(_header_info(rows).get("_titles", [])) + " " + name
        nt = _norm(title)
        if "hodinova praca" in nt:
            msgs.append(f"list „{name}“ preskočený (hodinová práca – používam 15-min dáta)")
            continue
        if "registrov" in nt or any("datum odpoctu" in [_norm(c) for c in _cells(r)] for r in rows[:30]):
            regs = parse_registers(rows)
            for r in regs:
                key = series or series_for(r["ident"], mapping)
                db.ensure_series(con, key, "sum", "kWh")
                con.execute(
                    "INSERT INTO registers(series_key, day, source, value, kind) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(series_key, day, source) DO UPDATE SET value=excluded.value, kind=excluded.kind",
                    (key, r["day"], "zsd", r["value"], r["kind"]))
            msgs.append(f"registre: {len(regs)} odpočtov")
            continue
        prof = parse_profile(rows, tz)
        if not prof["hours"]:
            continue
        key = series or series_for(prof["type"], mapping)
        db.ensure_series(con, key, "sum", "kWh")
        db.upsert_hourly(con, key, "zsd", prof["hours"].items())
        first, last = min(prof["hours"]), max(prof["hours"])
        total = sum(prof["hours"].values())
        msgs.append(
            f"{key} ({prof['type']}, EIC {prof['eic']}): {prof['intervals']} štvrťhodín → "
            f"{len(prof['hours'])} hodín, {total:.1f} kWh, "
            f"{datetime.fromtimestamp(first, tz):%d.%m.%Y} – {datetime.fromtimestamp(last, tz):%d.%m.%Y}")
    con.commit()
    db.log(con, "import zsd", f"{Path(path).name}: " + "; ".join(msgs))
    return msgs


def check_against_registers(con, series: str, tz: ZoneInfo) -> list[dict]:
    """Porovná súčet profilu medzi odpočtami so zmenou registra."""
    regs = con.execute("SELECT day, value FROM registers WHERE series_key=? AND source='zsd' ORDER BY day",
                       (series,)).fetchall()
    out = []
    for a, b in zip(regs, regs[1:]):
        # odpočet k dňu D = stav na konci dňa D
        start = datetime.fromisoformat(a["day"]).replace(tzinfo=tz) + timedelta(days=1)
        end = datetime.fromisoformat(b["day"]).replace(tzinfo=tz) + timedelta(days=1)
        s, e = int(start.timestamp()), int(end.timestamp())
        row = con.execute("SELECT COUNT(*) n, SUM(value) v FROM hourly WHERE series_key=? AND source='zsd' "
                          "AND ts>=? AND ts<?", (series, s, e)).fetchone()
        if not row["n"]:
            continue
        out.append({"od": a["day"], "do": b["day"], "register": b["value"] - a["value"],
                    "profil": round(row["v"], 3), "hodin": row["n"], "ocakavane": (e - s) // 3600})
    return out
