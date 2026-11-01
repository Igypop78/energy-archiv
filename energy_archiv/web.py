"""Jednoduché web rozhranie (Flask): prehľad, nahrávanie súborov, obnova do HA."""
from __future__ import annotations

import io
import threading
import traceback
from contextlib import redirect_stdout
from datetime import date, datetime

from flask import Flask, Response, redirect, render_template_string, request, url_for

from . import backup, db, deye, manual, restore, sync, zsd
from .cli import _ha, _local_ts, coverage

CSS = """
body{font-family:system-ui,sans-serif;margin:0;background:#f5f6f8;color:#1d2330}
header{background:#1d2330;color:#fff;padding:12px 20px}header a{color:#fff;margin-right:16px;text-decoration:none}
main{padding:16px 20px;max-width:1100px}table{border-collapse:collapse;width:100%;background:#fff;margin:8px 0 20px}
td,th{padding:6px 8px;border-bottom:1px solid #e3e6ea;text-align:left;font-size:14px}th{background:#eef0f3}
.num{text-align:right;font-variant-numeric:tabular-nums}.warn{color:#b45309}.ok{color:#15803d}
.card{background:#fff;padding:14px 16px;margin:0 0 16px;border-radius:8px;box-shadow:0 1px 2px #0001}
button,input[type=submit]{background:#2563eb;color:#fff;border:0;padding:7px 14px;border-radius:6px;cursor:pointer}
input,select{padding:5px}pre{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:6px;overflow:auto;font-size:13px}
.small{color:#64748b;font-size:13px}
"""

BASE = """<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Energetický archív</title>
<style>{{ css }}</style></head><body><header><b>⚡ Energetický archív</b> &nbsp;
<a href="{{ url_for('index') }}">Prehľad</a><a href="{{ url_for('imports') }}">Import</a>
<a href="{{ url_for('job') }}">Úloha</a></header><main>{{ body|safe }}</main></body></html>"""


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.name, self.buf, self.running = None, io.StringIO(), False

    @property
    def output(self):
        return self.buf.getvalue()

    def start(self, name, fn):
        with self.lock:
            if self.running:
                return False
            self.name, self.buf, self.running = name, io.StringIO(), True

        def run():
            buf = self.buf
            try:
                with redirect_stdout(buf):
                    fn()
            except Exception:
                buf.write("\nCHYBA:\n" + traceback.format_exc())
            finally:
                self.running = False

        threading.Thread(target=run, daemon=True).start()
        return True


def create_app(cfg) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
    job = Job()
    pwd = cfg["web"].get("password") or cfg.env.get("WEB_PASSWORD")

    def con():
        return db.connect(cfg["database"])

    def page(body, **ctx):
        return render_template_string(BASE, css=CSS, body=render_template_string(body, **ctx))

    def fmt(ts, f="%Y-%m-%d %H:%M"):
        return datetime.fromtimestamp(ts, cfg.tz).strftime(f) if ts else "—"

    @app.before_request
    def auth():
        if not pwd:
            return None
        a = request.authorization
        if not a or a.password != pwd:
            return Response("Prihlásenie", 401, {"WWW-Authenticate": 'Basic realm="energy-archiv"'})
        return None

    @app.route("/")
    def index():
        c = con()
        rows = []
        for s in db.series_list(c):
            n, mn, mx = c.execute("SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM hourly WHERE series_key=?",
                                  (s["key"],)).fetchone()
            m = c.execute("SELECT statistic_id FROM entity_map WHERE series_key=? AND valid_to IS NULL",
                          (s["key"],)).fetchone()
            rows.append(dict(s=s, n=n, od=fmt(mn, "%Y-%m-%d"), do=fmt(mx, "%Y-%m-%d"),
                             ent=m["statistic_id"] if m else "—"))
        logs = c.execute("SELECT * FROM log ORDER BY id DESC LIMIT 15").fetchall()
        return page("""
<div class="card"><form method="post" action="{{ url_for('run', what='sync') }}" style="display:inline">
<button>Synchronizovať z HA</button></form>
<form method="post" action="{{ url_for('run', what='backup') }}" style="display:inline"><button>Zálohovať na NAS</button></form>
<span class="small">&nbsp; Automaticky každú noc (systemd časovač).</span></div>
<table><tr><th>Veličina</th><th>Popis</th><th>Jedn.</th><th class="num">Hodín</th><th>Od</th><th>Do</th><th>Entita v HA</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('series', key=r.s.key) }}">{{ r.s.key }}</a></td><td>{{ r.s.label or '' }}</td>
<td>{{ r.s.unit or '' }}</td><td class="num">{{ r.n }}</td><td>{{ r.od }}</td><td>{{ r.do }}</td><td class="small">{{ r.ent }}</td></tr>
{% else %}<tr><td colspan=7>Zatiaľ prázdne – spusti synchronizáciu.</td></tr>{% endfor %}</table>
<h3>Záznam</h3><table>{% for l in logs %}<tr><td class="small">{{ fmt(l.ts) }}</td><td>{{ l.action }}</td><td class="small">{{ l.detail }}</td></tr>{% endfor %}</table>
""", rows=rows, logs=logs, fmt=lambda t: datetime.fromtimestamp(t, cfg.tz).strftime("%d.%m. %H:%M"))

    @app.route("/series/<key>")
    def series(key):
        c = con()
        s = c.execute("SELECT * FROM series WHERE key=?", (key,)).fetchone()
        maps = c.execute("SELECT * FROM entity_map WHERE series_key=? ORDER BY COALESCE(valid_from,0)", (key,)).fetchall()
        cov = coverage(cfg, c, key)
        return page("""
<h2>{{ s.key }} <span class="small">{{ s.label or '' }} · {{ s.unit or '' }} · priorita: {{ prio|join(' › ') }}</span></h2>
<div class="card"><b>Entity v HA</b><table><tr><th>statistic_id</th><th>od</th><th>do</th><th>poznámka</th></tr>
{% for m in maps %}<tr><td>{{ m.statistic_id }}</td><td>{{ fmt(m.valid_from) if m.valid_from else 'začiatok' }}</td>
<td>{{ fmt(m.valid_to) if m.valid_to else 'teraz' }}</td><td class="small">{{ m.note or '' }}</td></tr>{% endfor %}</table></div>
<div class="card"><b>Obnoviť do HA</b> <span class="small">– najprv sa zobrazí náhľad. Pred zápisom urob zálohu HA.</span>
<form method="post" action="{{ url_for('restore_preview', key=s.key) }}">
cieľ <input name="target" placeholder="aktuálna entita" size="34"> od <input name="od" type="date"> do <input name="do" type="date">
<input type="submit" value="Náhľad"></form></div>
<table><tr><th>Mesiac</th><th class="num">Súčet</th><th class="num">Hodín</th><th>Zdroje</th></tr>
{% for r in cov|reverse %}<tr><td>{{ r.mesiac }}</td><td class="num">{{ '%.2f'|format(r.sucet) }}</td>
<td class="num {{ '' if r.hodin == r.ocakavane else 'warn' }}">{{ r.hodin }}/{{ r.ocakavane }}</td>
<td class="small">{% for k, v in r.zdroje.items() %}{{ k }}: {{ v }} {% endfor %}</td></tr>{% endfor %}</table>
""", s=s, maps=maps, cov=cov, fmt=fmt, prio=cfg.priority(key))

    @app.route("/series/<key>/restore", methods=["POST"])
    def restore_preview(key):
        f = request.form
        od, do = _local_ts(cfg, f.get("od")), _local_ts(cfg, f.get("do"), end=True)
        target = f.get("target") or None
        try:
            with _ha(cfg) as ha:
                p = restore.plan(cfg, con(), ha, key, target, od, do)
        except (Exception, SystemExit) as e:
            return page("<div class='card warn'>{{ e }}</div>", e=e)
        return page("""
<h2>Náhľad obnovy: {{ p.series }} → {{ p.target }}</h2><div class="card">
Obdobie: <b>{{ fmt(p.first) }} – {{ fmt(p.last) }}</b> ({{ p.hours }} hodín, nulou doplnených {{ p.zero_filled }})<br>
Zdroje: {{ p.sources }}<br>{% if p.total is not none %}Spolu: <b>{{ '%.2f'|format(p.total) }} {{ p.unit }}</b> · {{ p.anchor }}{% endif %}
{% for w in p.warnings %}<p class="warn">⚠ {{ w }}</p>{% endfor %}
<form method="post" action="{{ url_for('restore_apply', key=p.series) }}">
<input type="hidden" name="target" value="{{ target or '' }}"><input type="hidden" name="od" value="{{ f.get('od','') }}">
<input type="hidden" name="do" value="{{ f.get('do','') }}">
<label><input type="checkbox" name="ok" required> Mám zálohu HA</label> <input type="submit" value="Zapísať do HA"></form></div>
""", p=p, fmt=fmt, target=target, f=f)

    @app.route("/series/<key>/apply", methods=["POST"])
    def restore_apply(key):
        f = request.form
        od, do = _local_ts(cfg, f.get("od")), _local_ts(cfg, f.get("do"), end=True)
        target = f.get("target") or None

        def go():
            with _ha(cfg) as ha:
                c = con()
                p = restore.plan(cfg, c, ha, key, target, od, do)
                print(f"zapísané {restore.apply(c, ha, p)} hodín do {p['target']} ({p['anchor']})")
        job.start(f"obnova {key}", go)
        return redirect(url_for("job"))

    @app.route("/run/<what>", methods=["POST"])
    def run(what):
        def do_sync():
            with _ha(cfg) as ha:
                r = sync.sync(cfg, con(), ha)
            print("\n".join(r["mapovanie"]))
            print(f"{r['veliciny']} veličín, {r['hodiny']} hodín")

        fns = {"sync": do_sync, "backup": lambda: print("\n".join(backup.run(cfg, con())))}
        job.start(what, fns[what])
        return redirect(url_for("job"))

    @app.route("/import", methods=["GET", "POST"])
    def imports():
        msgs = []
        if request.method == "POST":
            kind = request.form.get("kind")
            if kind == "zsd":
                for fs in request.files.getlist("files"):
                    try:
                        msgs += [f"{fs.filename}: {m}" for m in zsd.import_file(cfg, con(), fs.filename, fs.read())]
                    except Exception as e:
                        msgs.append(f"{fs.filename}: CHYBA {e}")
                msgs += [f"kontrola {r['od']}→{r['do']}: register {r['register']:.0f}, profil {r['profil']:.1f} kWh"
                         for key in ("grid_import", "grid_export")
                         for r in zsd.check_against_registers(con(), key, cfg.tz)[-3:]]
            elif kind == "manual":
                text = request.form.get("text") or ""
                fs = request.files.get("file")
                if fs and fs.filename:
                    text = fs.read().decode("utf-8-sig")
                msgs = manual.import_text(cfg, con(), text)
            elif kind == "deye":
                od, do = date.fromisoformat(request.form["od"]), date.fromisoformat(request.form["do"])
                job.start("import deye", lambda: print(deye.import_range(cfg, con(), od, do)))
                return redirect(url_for("job"))
        return page("""
{% if msgs %}<div class="card"><pre>{{ msgs|join('\n') }}</pre></div>{% endif %}
<div class="card"><b>ZSD – profil (15 min) a registre</b> <span class="small">.xls z portálu, aj viac naraz</span>
<form method="post" enctype="multipart/form-data"><input type="hidden" name="kind" value="zsd">
<input type="file" name="files" multiple accept=".xls,.xlsx,.csv"> <input type="submit" value="Nahrať"></form></div>
<div class="card"><b>Deye Cloud</b> <span class="small">5-min výkony → hodiny, doladené na denné súčty</span>
<form method="post"><input type="hidden" name="kind" value="deye">od <input type="date" name="od" required>
do <input type="date" name="do" value="{{ today }}"> <input type="submit" value="Stiahnuť"></form></div>
<div class="card"><b>Ručné hodnoty</b> <span class="small">velicina;obdobie;hodnota – napr. <code>gas;2025-03;85.2</code>,
<code>water;2025-01-01..2025-03-31;12.5</code>, oprava hodiny <code>grid_import;2025-10-26 14:00;1.25</code></span>
<form method="post" enctype="multipart/form-data"><input type="hidden" name="kind" value="manual">
<textarea name="text" rows="5" style="width:100%"></textarea><br><input type="file" name="file" accept=".csv,.txt">
<input type="submit" value="Uložiť"></form></div>
""", msgs=msgs, today=date.today().isoformat())

    @app.route("/job", endpoint="job")
    def job_page():
        return page("""{% if job.name %}<h3>{{ job.name }} {% if job.running %}– beží…{% else %}– hotovo{% endif %}</h3>
<pre>{{ job.output or '…' }}</pre>{% if job.running %}<script>setTimeout(()=>location.reload(),3000)</script>{% endif %}
{% else %}<p>Žiadna úloha.</p>{% endif %}""", job=job)

    return app
