"""Záloha archívu na NAS (konzistentná kópia SQLite + CSV) s rotáciou."""
from __future__ import annotations

import csv
import gzip
import os
import shutil
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from . import db


def export_csv(cfg, con, path: Path) -> int:
    """Všetky hodiny všetkých zdrojov + použitá hodnota – čitateľné aj bez tohto nástroja."""
    n = 0
    with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["velicina", "jednotka", "hodina_utc", "hodina_lokalne", "zdroj", "hodnota", "pouzita"])
        for s in db.series_list(con):
            best = db.best_hourly(con, s["key"], cfg.priority(s["key"]))
            for ts, src, val in con.execute(
                    "SELECT ts, source, value FROM hourly WHERE series_key=? ORDER BY ts, source", (s["key"],)):
                dt = datetime.fromtimestamp(ts, timezone.utc)
                w.writerow([s["key"], s["unit"], dt.isoformat(), dt.astimezone(cfg.tz).isoformat(), src,
                            round(val, 6), int(best.get(ts, (None, None))[1] == src)])
                n += 1
    return n


def _copy_to(src: Path, target: Path) -> None:
    """Skopíruje hotový súbor na NAS cez dočasné meno (SMB/NFS nezvládajú zámky SQLite)."""
    part = target.with_name(target.name + ".part")
    shutil.copyfile(src, part)
    os.replace(part, target)


def run(cfg, con) -> list[str]:
    bdir = Path(cfg["backup"]["dir"])
    bdir.mkdir(parents=True, exist_ok=True)
    local = Path(cfg["database"]).parent / "zaloha-tmp"
    local.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    target = bdir / f"archiv-{today}.sqlite"

    # 1) konzistentná kópia SQLite na lokálny disk, 2) hotový súbor skopírovať na NAS
    tmp_db = local / "archiv.sqlite"
    tmp_db.unlink(missing_ok=True)
    dst = sqlite3.connect(tmp_db)
    con.backup(dst)
    dst.close()
    _copy_to(tmp_db, target)
    msgs = [f"záloha {target} ({target.stat().st_size / 1e6:.1f} MB)"]

    tmp_csv = local / "archiv-posledny.csv.gz"
    rows = export_csv(cfg, con, tmp_csv)
    _copy_to(tmp_csv, bdir / "archiv-posledny.csv.gz")
    msgs.append(f"CSV export: {rows} riadkov")
    for f in (tmp_db, tmp_csv):
        f.unlink(missing_ok=True)
    for f in bdir.glob("*.tmp"):  # pozostatky starej verzie
        f.unlink(missing_ok=True)

    # rotácia: posledných N denných + prvá záloha každého mesiaca
    files = sorted(bdir.glob("archiv-????-??-??.sqlite"))
    keep = set(files[-int(cfg["backup"]["keep_daily"]):])
    months: dict[str, Path] = {}
    for f in files:
        months.setdefault(f.stem[7:14], f)
    keep |= set(sorted(months.values())[-int(cfg["backup"]["keep_monthly"]):])
    removed = [f for f in files if f not in keep]
    for f in removed:
        f.unlink()
    if removed:
        msgs.append(f"zmazaných starých záloh: {len(removed)}")
    db.log(con, "záloha", "; ".join(msgs))
    return msgs
