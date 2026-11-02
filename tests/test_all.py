"""Testy: ZSD (ak sú k dispozícii vzorové CSV), sync s výmenou entity, obnova, ručné hodnoty, záloha.

Spustenie: python3 tests/test_all.py [priečinok_so_vzorkami_zsd]
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_ha  # noqa: E402
from energy_archiv import backup, config, db, manual, restore, sync, zsd  # noqa: E402
from energy_archiv.ha import HA  # noqa: E402

H = 3600


def make_cfg(tmp):
    cfg = config.load(os.path.join(tmp, "nie.yaml"))
    cfg.data["database"] = os.path.join(tmp, "a.sqlite")
    cfg.data["backup"]["dir"] = os.path.join(tmp, "nas")
    cfg.data["ha"]["history_start"] = "2026-01-01"
    return cfg


def test_sync_restore(tmp):
    cfg = make_cfg(tmp)
    con = db.connect(cfg["database"])
    st = mock_ha.State()
    t0 = 1767225600  # 2026-01-01 00:00 UTC
    now = t0 + 200 * H
    # stará entita: 0.5 kWh/h počas 100 h, potom zanikla
    st.add_sum("sensor.grid_old")
    st.stats["sensor.grid_old"] = {t0 + i * H: 0.5 * (i + 1) for i in range(100)}
    st.add_sum("sensor.grid_old_cost", "EUR", None)
    st.stats["sensor.grid_old_cost"] = {t0 + i * H: 0.1 * (i + 1) for i in range(100)}
    st.add_sum("sensor.gas")
    st.stats["sensor.gas"] = {t0 + i * H: 0.2 * (i + 1) for i in range(200)}
    st.meta["sensor.gas"]["unit_class"] = "volume"
    st.meta["sensor.gas"]["statistics_unit_of_measurement"] = "m³"
    st.add_sum("sensor.solar_wh", "Wh")
    st.stats["sensor.solar_wh"] = {t0 + i * H: 1000.0 * (i + 1) for i in range(200)}
    st.prefs = {"energy_sources": [
        {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid_old", "stat_cost": None,
                                        "entity_energy_price": None}], "flow_to": []},
        {"type": "solar", "stat_energy_from": "sensor.solar_wh"},
        {"type": "gas", "stat_energy_from": "sensor.gas", "stat_cost": None}]}
    srv, port = mock_ha.start(st)
    with HA(f"http://127.0.0.1:{port}", "test") as ha:
        r = sync.sync(cfg, con, ha, now=t0 + 100 * H)
    keys = {s["key"] for s in db.series_list(con)}
    assert keys == {"grid_import", "grid_import_cost", "solar", "gas"}, keys
    solar = db.best_hourly(con, "solar", ["ha"])
    assert abs(sum(v for v, _ in solar.values()) - 100.0) < 1e-6, "Wh → kWh"  # 100 h * 1 kWh (prvá hod. = sum)
    assert len(db.best_hourly(con, "grid_import", ["ha"])) == 100

    # výmena entity: nová od hodiny 120 (sum začína 0), stará stratená
    st.add_sum("sensor.grid_new")
    st.stats["sensor.grid_new"] = {t0 + (120 + i) * H: 0.7 * i for i in range(80)}
    st.prefs["energy_sources"][0]["flow_from"][0]["stat_energy_from"] = "sensor.grid_new"
    del st.stats["sensor.grid_old"]
    with HA(f"http://127.0.0.1:{port}", "test") as ha:
        r = sync.sync(cfg, con, ha, now=now)
    assert any("grid_old → sensor.grid_new" in n for n in r["mapovanie"]), r
    gi = db.best_hourly(con, "grid_import", ["ha"])
    assert len(gi) == 180, len(gi)

    # ZSD má presnejšie dáta pre hodiny 0–9 → majú prednosť
    db.upsert_hourly(con, "grid_import", "zsd", [(t0 + i * H, 0.6) for i in range(10)])
    con.commit()

    # obnova celej histórie do novej entity; HA medzitým zapísal ďalšiu hodinu (200), ktorú archív ešte nemá
    st.stats["sensor.grid_new"][t0 + 200 * H] = 0.7 * 80
    with HA(f"http://127.0.0.1:{port}", "test") as ha:
        p = restore.plan(cfg, con, ha, "grid_import")
        assert p["target"] == "sensor.grid_new"
        assert p["zero_filled"] == 20  # hodiny 100–119 bez dát
        assert p["sources"] == {"zsd": 10, "ha": 170}, p["sources"]
        n = restore.apply(con, ha, p)
    s = st.stats["sensor.grid_new"]
    assert n == 200 and len(s) == 201
    assert abs(s[t0 + 200 * H] - s[t0 + 199 * H] - 0.7) < 1e-9, "hodina po obnove pokračuje bez skoku"
    hrs = sorted(s)[:200]
    ch = [s[b] - s[a] for a, b in zip(hrs, hrs[1:])]
    assert all(abs(c - 0.6) < 1e-9 for c in ch[:9])      # hodiny 1–9 zo ZSD
    assert abs(ch[9] - 0.5) < 1e-9                         # hodina 10 z HA (stará entita)
    assert all(abs(c) < 1e-9 for c in ch[99:119])          # výpadok 100–119 = 0
    assert abs(ch[120] - 0.7) < 1e-9                       # nová entita
    print("  obnova: posledný súčet", round(s[hrs[-1]], 3), "anchor:", p["anchor"])
    assert abs(s[hrs[0]] - 0.6) < 1e-9, "prvá hodina histórie musí začínať od 0 (+ jej prírastok)"
    assert abs(s[hrs[-1]] - 106.3) < 1e-9
    assert "unit_class" not in st.imports[-1]  # záložná varianta metadát po odmietnutí

    # čiastočná obnova so súčtami v HA po nej – nadviazanie na koniec
    st.stats["sensor.gas"] = {t0 + i * H: 100 + 0.2 * i for i in range(150, 200)}  # stratená história do 150
    with HA(f"http://127.0.0.1:{port}", "test") as ha:
        p = restore.plan(cfg, con, ha, "gas", end=t0 + 150 * H)
        restore.apply(con, ha, p)
    g = st.stats["sensor.gas"]
    assert abs(g[t0 + 150 * H] - g[t0 + 149 * H] - 0.2) < 1e-9, (g[t0 + 149 * H], g[t0 + 150 * H])
    assert p["adjust"] and "posunú" in p["anchor"], p["anchor"]
    assert abs(g[t0] - 0.2) < 1e-9 or g[t0] < 1, "história plynu začína od 0"
    # nákladová entita s iným názvom, známa len z energy/info
    st.add_sum("sensor.plyn_naklady_auto", "EUR", None)
    st.stats["sensor.plyn_naklady_auto"] = {t0 + 199 * H: 1.0}
    st.cost_sensors = {"sensor.gas": "sensor.plyn_naklady_auto"}
    with HA(f"http://127.0.0.1:{port}", "test") as ha:
        sync.sync(cfg, con, ha, now=now)
    m = con.execute("SELECT statistic_id FROM entity_map WHERE series_key='gas_cost' AND valid_to IS NULL").fetchone()
    assert m and m["statistic_id"] == "sensor.plyn_naklady_auto", m
    srv.shutdown()
    print("OK sync + výmena entity + obnova + cost_sensors")
    return cfg, con


def test_manual(cfg, con):
    t0 = 1767225600
    msgs = manual.import_text(cfg, con, "velicina;obdobie;hodnota\nwater;2026-02;30\ngas;2026-01;100\n")
    print("  ", msgs)
    w = db.best_hourly(con, "water", cfg.priority("water"))
    assert abs(sum(v for v, _ in w.values()) - 30) < 1e-6 and len(w) == 28 * 24
    msgs = manual.import_text(cfg, con, "grid_import;2026-01-01 05:00;9.9")
    gi = db.best_hourly(con, "grid_import", cfg.priority("grid_import"))
    from datetime import datetime
    ts = int(datetime(2026, 1, 1, 5, tzinfo=cfg.tz).timestamp())
    assert gi[ts] == (9.9, "oprava")
    print("OK ručné hodnoty")


def test_backup(cfg, con):
    msgs = backup.run(cfg, con)
    assert any("záloha" in m for m in msgs)
    print("OK záloha:", msgs)


def test_zsd(sample_dir):
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg(tmp)
        con = db.connect(cfg["database"])
        for f in sorted(Path(sample_dir).glob("*.csv")):
            for m in zsd.import_file(cfg, con, f):
                print("  ", f.name[:20], m)
        checks = zsd.check_against_registers(con, "grid_export", cfg.tz)
        oct_ = [c for c in checks if c["do"] == "2026-10-31"][0]
        assert abs(oct_["register"] - oct_["profil"]) < 1 and oct_["hodin"] == 745, oct_
        print("OK ZSD profil = register:", oct_)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        cfg, con = test_sync_restore(tmp)
        test_manual(cfg, con)
        test_backup(cfg, con)
    if len(sys.argv) > 1:
        test_zsd(sys.argv[1])
