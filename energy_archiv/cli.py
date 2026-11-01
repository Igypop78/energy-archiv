"""Príkazový riadok: python -m energy_archiv <príkaz>."""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import __version__, backup, config, db, deye, manual, restore, sync, zsd
from .ha import HA


def _ha(cfg) -> HA:
    url, token = cfg.env.get("HA_URL"), cfg.env.get("HA_TOKEN")
    if not url or not token:
        sys.exit("V .env chýba HA_URL alebo HA_TOKEN")
    return HA(url, token)


def _local_ts(cfg, s: str | None, end=False) -> int | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    if len(s) == 10 and end:
        d = d.replace(hour=23)
    return int(d.replace(tzinfo=cfg.tz).timestamp()) + (3600 if end else 0)


def coverage(cfg, con, series: str) -> list[dict]:
    """Mesačný prehľad: hodiny, súčet a zdroje použitých hodnôt."""
    best = db.best_hourly(con, series, cfg.priority(series))
    out: dict[str, dict] = {}
    for ts, (v, src) in best.items():
        m = datetime.fromtimestamp(ts, cfg.tz).strftime("%Y-%m")
        r = out.setdefault(m, {"mesiac": m, "hodin": 0, "sucet": 0.0, "zdroje": {}})
        r["hodin"] += 1
        r["sucet"] += v
        r["zdroje"][src] = r["zdroje"].get(src, 0) + 1
    for m, r in out.items():
        y, mo = map(int, m.split("-"))
        a = datetime(y, mo, 1, tzinfo=cfg.tz)
        b = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=cfg.tz)
        r["ocakavane"] = int((b.timestamp() - a.timestamp()) // 3600)
    return [out[k] for k in sorted(out)]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="energy-archiv", description="Energetický archív pre Home Assistant")
    ap.add_argument("-c", "--config", help="cesta ku config.yaml")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="veličiny, entity a pokrytie")
    p = sub.add_parser("sync", help="stiahnuť štatistiky z HA")
    p.add_argument("--full", action="store_true", help="znova celú históriu")
    sub.add_parser("backup", help="záloha na NAS")
    sub.add_parser("nightly", help="sync + záloha (pre časovač)")
    p = sub.add_parser("import-zsd", help="import .xls/.csv z portálu ZSD")
    p.add_argument("files", nargs="+")
    p.add_argument("--series", help="vynútiť veličinu (inak podľa typu profilu)")
    sub.add_parser("check-zsd", help="porovnať profil ZSD s odpočtami registrov")
    sub.add_parser("deye-stanice", help="overiť prihlásenie do Deye Cloud a vypísať stanice")
    p = sub.add_parser("ha-statistiky", help="štatistiky v HA vrátane starých entít (s rozsahom dát)")
    p.add_argument("filter", nargs="?", help="časť názvu, napr. import alebo energy")
    p = sub.add_parser("pripoj", help="pripojiť starú entitu z HA k veličine (história pred aktuálnou)")
    p.add_argument("series")
    p.add_argument("statistic_id")
    p = sub.add_parser("import-deye", help="import histórie z Deye Cloud")
    p.add_argument("--od", required=True, help="RRRR-MM-DD")
    p.add_argument("--do", default=date.today().isoformat())
    p.add_argument("--znova", action="store_true", help="stiahnuť aj dni, ktoré už sú v archíve")
    p = sub.add_parser("import-manual", help="ručné hodnoty z CSV (faktúry, opravy)")
    p.add_argument("file")
    p = sub.add_parser("pokrytie", help="mesačný prehľad veličiny")
    p.add_argument("series")
    p = sub.add_parser("mapuj", help="ručne priradiť entitu k veličine")
    p.add_argument("series")
    p.add_argument("statistic_id")
    p.add_argument("--od", help="od kedy platí (RRRR-MM-DD HH:MM, miestny čas); bez = od začiatku")
    p = sub.add_parser("obnov", help="nahrať históriu z archívu do HA")
    p.add_argument("series")
    p.add_argument("--target", help="statistic_id v HA (predvolene aktuálna entita)")
    p.add_argument("--od")
    p.add_argument("--do")
    p.add_argument("--apply", action="store_true", help="naozaj zapísať (inak len náhľad)")
    p = sub.add_parser("web", help="spustiť web rozhranie")
    p.add_argument("--port", type=int)
    a = ap.parse_args(argv)

    cfg = config.load(a.config)
    con = db.connect(cfg["database"])

    if a.cmd == "status":
        for s in db.series_list(con):
            n, mn, mx = con.execute("SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM hourly WHERE series_key=?",
                                    (s["key"],)).fetchone()
            rng = (f"{datetime.fromtimestamp(mn, cfg.tz):%Y-%m-%d} – {datetime.fromtimestamp(mx, cfg.tz):%Y-%m-%d}"
                   if mn else "bez dát")
            print(f"{s['key']:<28} {s['unit'] or '':<4} {n:>7} h  {rng}  {s['label'] or ''}")
            for m in con.execute("SELECT * FROM entity_map WHERE series_key=? ORDER BY COALESCE(valid_from,0)",
                                 (s["key"],)):
                vf = datetime.fromtimestamp(m["valid_from"], cfg.tz).strftime("%Y-%m-%d %H:%M") if m["valid_from"] else "začiatok"
                vt = datetime.fromtimestamp(m["valid_to"], cfg.tz).strftime("%Y-%m-%d %H:%M") if m["valid_to"] else "teraz"
                print(f"    ↳ {m['statistic_id']}  ({vf} → {vt})")
    elif a.cmd in ("sync", "nightly"):
        with _ha(cfg) as ha:
            r = sync.sync(cfg, con, ha, full=getattr(a, "full", False))
        for n in r["mapovanie"]:
            print("mapovanie:", n)
        print(f"sync: {r['veliciny']} veličín, {r['hodiny']} hodín")
        if a.cmd == "nightly":
            for m in backup.run(cfg, con):
                print(m)
    elif a.cmd == "backup":
        for m in backup.run(cfg, con):
            print(m)
    elif a.cmd == "import-zsd":
        for f in a.files:
            for m in zsd.import_file(cfg, con, f, series=a.series):
                print(f"{Path(f).name}: {m}")
    elif a.cmd == "check-zsd":
        for key in sorted({r[0] for r in con.execute("SELECT DISTINCT series_key FROM registers")}):
            for r in zsd.check_against_registers(con, key, cfg.tz):
                ok = "OK " if abs(r["register"] - r["profil"]) < 1.5 and r["hodin"] == r["ocakavane"] else "!! "
                print(f"{ok}{key} {r['od']} → {r['do']}: register {r['register']:.0f} kWh, profil "
                      f"{r['profil']:.1f} kWh, hodín {r['hodin']}/{r['ocakavane']}")
    elif a.cmd == "deye-stanice":
        for s in deye.Deye.from_cfg(cfg).stations():
            t = s.get("startOperatingTime")
            od = datetime.fromtimestamp(float(t), cfg.tz).strftime("%Y-%m-%d") if t else "?"
            print(f"id {s.get('id')}: {s.get('name')}  (v prevádzke od {od})")
    elif a.cmd == "ha-statistiky":
        mapped = {r["statistic_id"]: r["series_key"] for r in db.all_maps(con)}
        now = int(time.time()) // 3600 * 3600
        hist = sync._parse_day(cfg["ha"]["history_start"])
        with _ha(cfg) as ha:
            ids = sorted(ha.list_statistic_ids(), key=lambda m: m["statistic_id"])
            if a.filter:
                ids = [m for m in ids if a.filter.lower() in m["statistic_id"].lower()]
            for m in ids:
                sid = m["statistic_id"]
                kind = "sum" if m.get("has_sum") else "mean"
                line = f"{sid:<60} {m.get('statistics_unit_of_measurement') or '':<8}"
                if a.filter:
                    first = sync.first_hour(ha, sid, hist, now + 3600, kind)
                    last = sync.last_hour(ha, sid, hist, now + 3600, kind) if first else None
                    f = lambda t: datetime.fromtimestamp(t, cfg.tz).strftime("%Y-%m-%d")
                    line += f" {f(first) + ' – ' + f(last) if first else 'bez hodinových dát':<25}"
                if sid in mapped:
                    line += f" ← {mapped[sid]}"
                print(line)
        if not a.filter:
            print("\nTip: energy-archiv ha-statistiky import   (s filtrom sa ukáže aj rozsah dát)")
    elif a.cmd == "pripoj":
        maps = con.execute("SELECT * FROM entity_map WHERE series_key=? ORDER BY COALESCE(valid_from,0)",
                           (a.series,)).fetchall()
        if not maps:
            sys.exit(f"veličina {a.series} neexistuje – najprv spusti sync")
        if a.statistic_id in {m["statistic_id"] for m in maps}:
            sys.exit("táto entita už je k veličine pripojená")
        cur = maps[0]
        now = int(time.time()) // 3600 * 3600
        hist = sync._parse_day(cfg["ha"]["history_start"])
        kind = con.execute("SELECT kind FROM series WHERE key=?", (a.series,)).fetchone()["kind"]
        with _ha(cfg) as ha:
            switch = cur["valid_from"] or sync.first_hour(ha, cur["statistic_id"], hist, now + 3600, kind)
            old_first = sync.first_hour(ha, a.statistic_id, hist, now + 3600, kind)
        if not switch or not old_first:
            sys.exit("v HA sa nenašli hodinové dáta")
        if old_first >= switch:
            sys.exit("stará entita nemá dáta pred aktuálnou – nie je čo pripojiť")
        con.execute("UPDATE entity_map SET valid_from=? WHERE id=?", (switch, cur["id"]))
        con.execute("INSERT INTO entity_map(series_key, statistic_id, valid_from, valid_to, note, created) "
                    "VALUES (?,?,?,?,?,?)", (a.series, a.statistic_id, None, switch, "pripojená stará", int(time.time())))
        con.commit()
        db.log(con, "mapovanie", f"{a.series}: pripojená {a.statistic_id} do "
                                 f"{datetime.fromtimestamp(switch, cfg.tz):%Y-%m-%d %H:%M}")
        print(f"pripojené: {a.statistic_id} → {a.series} (do {datetime.fromtimestamp(switch, cfg.tz):%Y-%m-%d %H:%M}). "
              "Spusti: energy-archiv sync")
    elif a.cmd == "import-deye":
        r = deye.import_range(cfg, con, date.fromisoformat(a.od), date.fromisoformat(a.do), redo=a.znova)
        print(r)
    elif a.cmd == "import-manual":
        for m in manual.import_text(cfg, con, Path(a.file).read_text(encoding="utf-8-sig")):
            print(m)
    elif a.cmd == "pokrytie":
        for r in coverage(cfg, con, a.series):
            z = ", ".join(f"{k}:{v}" for k, v in r["zdroje"].items())
            flag = "" if r["hodin"] == r["ocakavane"] else f"  chýba {r['ocakavane'] - r['hodin']} h"
            print(f"{r['mesiac']}  {r['sucet']:>10.2f}  {r['hodin']:>4}/{r['ocakavane']} h  [{z}]{flag}")
    elif a.cmd == "mapuj":
        db.ensure_series(con, a.series)
        vf = _local_ts(cfg, a.od)
        cur = con.execute("SELECT * FROM entity_map WHERE series_key=? AND valid_to IS NULL", (a.series,)).fetchone()
        if cur:
            con.execute("UPDATE entity_map SET valid_to=? WHERE id=?", (vf or int(time.time()) // 3600 * 3600, cur["id"]))
        con.execute("INSERT INTO entity_map(series_key, statistic_id, valid_from, note, created) VALUES (?,?,?,?,?)",
                    (a.series, a.statistic_id, vf, "ručne", int(time.time())))
        con.commit()
        db.log(con, "mapovanie", f"{a.series} ← {a.statistic_id} (ručne)")
        print("uložené – spusti sync")
    elif a.cmd == "obnov":
        with _ha(cfg) as ha:
            p = restore.plan(cfg, con, ha, a.series, a.target, _local_ts(cfg, a.od), _local_ts(cfg, a.do, end=True))
            f = lambda t: datetime.fromtimestamp(t, cfg.tz).strftime("%Y-%m-%d %H:%M")
            print(f"{p['series']} → {p['target']} [{p['unit']}]")
            print(f"  obdobie {f(p['first'])} – {f(p['last'])}, {p['hours']} hodín, doplnených nulou {p['zero_filled']}")
            print(f"  zdroje: {p['sources']}")
            if p["total"] is not None:
                print(f"  spolu {p['total']:.2f} {p['unit']}; {p['anchor']}")
            for w in p["warnings"]:
                print("  POZOR:", w)
            if a.apply:
                print(f"zapísané: {restore.apply(con, ha, p)} hodín")
            else:
                print("náhľad – na zápis pridaj --apply (pred tým urob zálohu HA)")
    elif a.cmd == "web":
        from .web import create_app
        app = create_app(cfg)
        app.run(host=cfg["web"]["host"], port=a.port or cfg["web"]["port"])


if __name__ == "__main__":
    main()
