"""Klient Home Assistant WebSocket API pre štatistiky a nastavenia Energy panela."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .wsclient import WebSocket


class HAError(RuntimeError):
    pass


def _ts(value) -> int:
    """HA vracia 'start' ako ms (novšie verzie) alebo ISO reťazec (staršie)."""
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 1e11 else int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class HA:
    def __init__(self, url: str, token: str, timeout: float = 120):
        url = url.rstrip("/")
        ws_url = url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        self.ws = WebSocket(ws_url, timeout=timeout)
        self._id = 0
        msg = json.loads(self.ws.recv())
        if msg.get("type") != "auth_required":
            raise HAError(f"neočakávaná odpoveď: {msg}")
        self.ws.send(json.dumps({"type": "auth", "access_token": token}))
        msg = json.loads(self.ws.recv())
        if msg.get("type") != "auth_ok":
            raise HAError("prihlásenie do HA zlyhalo – skontroluj HA_TOKEN")
        self.version = msg.get("ha_version")

    def close(self):
        self.ws.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, type_: str, **payload):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "type": type_, **payload}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") != mid or msg.get("type") != "result":
                continue
            if not msg.get("success"):
                err = msg.get("error") or {}
                raise HAError(f"{type_}: {err.get('code')} {err.get('message')}")
            return msg.get("result")

    # ---- vyššie funkcie
    def energy_prefs(self) -> dict:
        return self.call("energy/get_prefs")

    def list_statistic_ids(self, statistic_type: str | None = None) -> list[dict]:
        kw = {"statistic_type": statistic_type} if statistic_type else {}
        return self.call("recorder/list_statistic_ids", **kw)

    def metadata(self, statistic_ids: list[str]) -> dict[str, dict]:
        res = self.call("recorder/get_statistics_metadata", statistic_ids=statistic_ids)
        return {m["statistic_id"]: m for m in res}

    def hourly(self, statistic_ids: list[str], start: int, end: int,
               types: list[str]) -> dict[str, list[dict]]:
        """Hodinové štatistiky; 'start' sa vráti ako unix sekundy (UTC)."""
        res = self.call("recorder/statistics_during_period", start_time=iso(start), end_time=iso(end),
                        statistic_ids=statistic_ids, period="hour", types=types)
        out = {}
        for sid, rows in (res or {}).items():
            for r in rows:
                r["start"] = _ts(r["start"])
            out[sid] = rows
        return out

    def import_statistics(self, metadata: dict, stats: list[dict]) -> None:
        self.call("recorder/import_statistics", metadata=metadata, stats=stats)
