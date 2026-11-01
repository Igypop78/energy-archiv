"""Import histórie z Deye Cloud OpenAPI.

* /station/history granularity=1 → ~5-min výkony (W) za jeden deň → hodinové kWh
* /station/history granularity=2 → denné súčty (kWh), max. 30 dní na dotaz
Hodinové hodnoty sa pri dostatočnom pokrytí dňa doladia na denný súčet Deye.

Prihlasovacie údaje v .env: DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL, DEYE_PASSWORD
"""
from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from . import db

SUCCESS = "1000000"
RATE_LIMIT_CODES = {"1001001"}
# pole výkonu (frames) → pole denného súčtu (granularity 2)
DAILY_FIELD = {
    "generationPower": "generationValue",
    "purchasePower": "purchaseValue",
    "gridPower": "gridValue",
    "chargePower": "chargeValue",
    "dischargePower": "dischargeValue",
    "consumptionPower": "consumptionValue",
}


class DeyeError(RuntimeError):
    pass


class Deye:
    def __init__(self, base_url, app_id, app_secret, email, password=None, password_sha256=None,
                 delay: float = 1.5):
        if not (app_id and app_secret and email and (password or password_sha256)):
            raise DeyeError("chýba DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL alebo DEYE_PASSWORD v .env")
        self.base = base_url.rstrip("/") + "/v1.0"
        self.app_id, self.app_secret, self.email = app_id, app_secret, email
        self.pwd = (password_sha256 or hashlib.sha256(password.encode()).hexdigest()).lower()
        self.delay = delay
        self.s = requests.Session()
        self.token, self.token_exp = None, 0.0
        self.limited = 0

    @classmethod
    def from_cfg(cls, cfg):
        e = cfg.env
        return cls(e.get("DEYE_BASE_URL") or cfg["deye"]["base_url"], e.get("DEYE_APP_ID"),
                   e.get("DEYE_APP_SECRET"), e.get("DEYE_EMAIL"), e.get("DEYE_PASSWORD"),
                   e.get("DEYE_PASSWORD_SHA256"))

    def _post(self, path, body, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._token()}"
        err = None
        limit_hits = 0
        for attempt in range(8):
            if attempt and not limit_hits:
                time.sleep(min(2 ** attempt, 30))
            try:
                r = self.s.post(f"{self.base}/{path}", json=body, headers=headers, timeout=30)
            except requests.RequestException as e:
                err = e
                continue
            if r.status_code == 429 or r.status_code >= 500:
                err = DeyeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            data = r.json()
            code = str(data.get("code", SUCCESS))
            if code in RATE_LIMIT_CODES or "too many" in str(data.get("msg", "")).lower():
                self.limited += 1
                wait = min(60 * (1 + limit_hits), 300)
                limit_hits += 1
                print(f"   limit Deye API – čakám {wait} s", flush=True)
                time.sleep(wait)
                self.delay = min(self.delay * 1.5, 10)  # trvalo spomaliť
                err = DeyeError(f"{path}: {code} {data.get('msg')}")
                continue
            if data.get("success") is False or code != SUCCESS:
                raise DeyeError(f"{path}: {code} {data.get('msg')}")
            time.sleep(self.delay)
            return data
        raise DeyeError(f"{path}: {err}")

    def _token(self):
        if self.token and time.time() < self.token_exp - 60:
            return self.token
        d = self._post(f"account/token?appId={self.app_id}",
                       {"appSecret": self.app_secret, "email": self.email, "password": self.pwd}, auth=False)
        tok = d.get("accessToken") or (d.get("data") or {}).get("accessToken")
        if not tok:
            raise DeyeError(f"token: {d.get('msg')}")
        self.token = tok[7:] if tok.lower().startswith("bearer ") else tok
        self.token_exp = time.time() + float(d.get("expiresIn") or 3600)
        return self.token

    def stations(self):
        return self._post("station/list", {"page": 1, "size": 100}).get("stationList") or []

    def frames(self, station_id, day: date):
        return self._post("station/history", {"stationId": station_id, "granularity": 1,
                                              "startAt": day.isoformat(),
                                              "endAt": (day + timedelta(days=1)).isoformat()}
                          ).get("stationDataItems") or []

    def daily(self, station_id, start: date, end_excl: date):
        return self._post("station/history", {"stationId": station_id, "granularity": 2,
                                              "startAt": start.isoformat(), "endAt": end_excl.isoformat()}
                          ).get("stationDataItems") or []


def _f(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _ts(v, tz) -> datetime | None:
    n = _f(v)
    if n is None:
        try:
            dt = datetime.fromisoformat(str(v))
            return dt if dt.tzinfo else dt.replace(tzinfo=tz)
        except ValueError:
            return None
    if n > 1e10:
        n /= 1000
    return datetime.fromtimestamp(n, tz)


def frames_to_hourly(items, tz: ZoneInfo, fields, divisor=1000.0) -> tuple[dict, dict]:
    """Integruje výkony podľa skutočných časových rozostupov → {pole: {utc_hodina: kWh}}, pokrytie."""
    # hneď do UTC: pri zmene času sa miestne 02:xx opakuje a porovnanie by ignorovalo „fold“
    pts = sorted(((t.astimezone(timezone.utc), it) for it in items if (t := _ts(it.get("timeStamp"), tz))),
                 key=lambda x: x[0])
    out = {f: defaultdict(float) for f in fields}
    covered = defaultdict(float)  # sekundy pokryté dátami v hodine
    for (t0, a), (t1, _b) in zip(pts, pts[1:]):
        dt = (t1 - t0).total_seconds()
        if dt <= 0 or dt > 20 * 60:  # medzera > 20 min sa neinterpoluje
            continue
        # úsek priradíme hodinám (môže pretínať hranicu hodiny)
        u0 = t0.timestamp()
        u1 = u0 + dt
        while u0 < u1:
            h = int(u0) // 3600 * 3600
            seg = min(u1, h + 3600) - u0
            covered[h] += seg
            for f in fields:
                p = abs(_f(a.get(f)) or 0.0)  # prázdne pole = 0 W; Deye dáva nabíjanie/predaj so znamienkom −
                out[f][h] += p / divisor * seg / 3600
            u0 += seg
    return {f: dict(v) for f, v in out.items()}, dict(covered)


def import_range(cfg, con, start: date, end: date, client: Deye | None = None, log=None,
                 redo: bool = False) -> dict:
    log = log or (lambda m: print(m, flush=True))
    tz = cfg.tz
    dcfg = cfg["deye"]
    fmap = {f: s for f, s in (dcfg["map"] or {}).items() if s}
    client = client or Deye.from_cfg(cfg)
    sid = dcfg.get("station_id")
    if not sid:
        st = client.stations()
        if not st:
            raise DeyeError("na účte nie je žiadna stanica")
        sid = st[0]["id"]
        log(f"stanica: {st[0].get('name')} (id {sid})")
    for s in set(fmap.values()):
        db.ensure_series(con, s, "sum", "kWh")

    # denné súčty
    daily: dict[str, dict[str, float]] = defaultdict(dict)
    d = start
    while d <= end:
        e = min(d + timedelta(days=30), end + timedelta(days=1))
        try:
            items = client.daily(sid, d, e)
        except Exception as ex:
            log(f"denné súčty {d} – {e}: CHYBA {ex}")
            items = []
        for it in items:
            day = _ts(it.get("timeStamp"), tz)
            if not day and it.get("year"):
                day = datetime(int(it["year"]), int(it.get("month", 1)), int(it.get("day", 1)), tzinfo=tz)
            if not day:
                continue
            for pf, series in fmap.items():
                v = _f(it.get(DAILY_FIELD.get(pf, "")))
                if v is not None:
                    v = abs(v)
                    daily[day.date().isoformat()][series] = v
                    con.execute("INSERT INTO daily_ref(series_key, day, source, value) VALUES (?,?,?,?) "
                                "ON CONFLICT DO UPDATE SET value=excluded.value",
                                (series, day.date().isoformat(), "deye", v))
        d = e
    con.commit()

    stats = {"dni": 0, "preskocene": 0, "hodiny": 0, "doladene": 0}
    failed: list[str] = []
    first_series = next(iter(fmap.values()))
    d = start
    streak = 0
    while d <= end:
        day_start = int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())
        if not redo:
            n = con.execute("SELECT COUNT(*) FROM hourly WHERE series_key=? AND source='deye' AND ts>=? AND ts<?",
                            (first_series, day_start, day_start + 86400)).fetchone()[0]
            if n >= 20:
                stats["preskocene"] += 1
                d += timedelta(days=1)
                continue
        items, err = None, None
        for attempt in range(3):
            try:
                items = client.frames(sid, d)
                break
            except Exception as e:  # sieť, limit API, neplatná odpoveď
                err = e
                time.sleep(5 * (attempt + 1))
        if items is None:
            failed.append(d.isoformat())
            streak += 1
            log(f"{d}: CHYBA {err}")
            if streak >= 10:
                log("10 chýb za sebou – import zastavený, skús neskôr znova (stiahnuté dni sa preskočia)")
                break
            d += timedelta(days=1)
            continue
        streak = 0
        try:
            hourly, covered = frames_to_hourly(items, tz, list(fmap), float(dcfg["power_divisor"]))
        except Exception as e:
            failed.append(d.isoformat())
            log(f"{d}: CHYBA spracovania {e!r}")
            d += timedelta(days=1)
            continue
        cover = sum(covered.values()) / 86400
        for pf, series in fmap.items():
            hrs = hourly.get(pf) or {}
            if not hrs:
                continue
            ref = daily.get(d.isoformat(), {}).get(series)
            tot = sum(hrs.values())
            if ref is not None and tot > 0 and cover >= 0.8:
                k = ref / tot
                if 0.5 < k < 2:
                    hrs = {h: v * k for h, v in hrs.items()}
                    stats["doladene"] += 1
            stats["hodiny"] += db.upsert_hourly(con, series, "deye", hrs.items())
        con.commit()
        stats["dni"] += 1
        log(f"{d}: {len(items)} záznamov, pokrytie {cover:.0%}")
        d += timedelta(days=1)
    stats["chyby"] = failed
    if failed:
        log(f"nestiahnuté dni ({len(failed)}): {', '.join(failed[:30])}{' …' if len(failed) > 30 else ''}")
    db.log(con, "import deye", f"{start} – {end}: {stats}")
    return stats
