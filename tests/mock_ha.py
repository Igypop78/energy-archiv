"""Simulovaný Home Assistant (WebSocket API) na testy – len štandardná knižnica."""
from __future__ import annotations

import base64
import hashlib
import json
import socketserver
import struct
import threading
from datetime import datetime


class State:
    def __init__(self):
        self.prefs = {"energy_sources": [], "device_consumption": []}
        self.stats: dict[str, dict[int, float]] = {}   # sid -> {ts: sum}
        self.means: dict[str, dict[int, float]] = {}
        self.meta: dict[str, dict] = {}
        self.imports: list = []
        self.adjusts: list = []

    def add_sum(self, sid, unit="kWh", unit_class="energy"):
        self.stats.setdefault(sid, {})
        self.meta[sid] = {"statistic_id": sid, "has_sum": True, "has_mean": False, "mean_type": 0,
                          "name": sid, "source": "recorder", "statistics_unit_of_measurement": unit,
                          "display_unit_of_measurement": unit, "unit_class": unit_class}


def _ts(s):
    return int(datetime.fromisoformat(s).timestamp())


def handle(state: State, msg: dict):
    t = msg["type"]
    if t == "energy/get_prefs":
        return state.prefs
    if t == "recorder/list_statistic_ids":
        return list(state.meta.values())
    if t == "recorder/get_statistics_metadata":
        return [state.meta[s] for s in msg["statistic_ids"] if s in state.meta]
    if t == "recorder/statistics_during_period":
        a, b = _ts(msg["start_time"]), _ts(msg["end_time"])
        out = {}
        for sid in msg["statistic_ids"]:
            if sid in state.means:
                rows = [{"start": ts * 1000, "end": (ts + 3600) * 1000, "mean": v}
                        for ts, v in sorted(state.means[sid].items()) if a <= ts < b]
            else:
                from energy_archiv.sync import FACTORS
                m = state.meta.get(sid, {})
                uc, su = m.get("unit_class"), m.get("statistics_unit_of_measurement")
                want = (msg.get("units") or {}).get(uc)
                k = FACTORS[uc][su] / FACTORS[uc][want] if uc in FACTORS and want else 1.0
                series = [(ts, v * k) for ts, v in sorted(state.stats.get(sid, {}).items())]
                rows, prev = [], None
                for ts, s in series:
                    if ts < a:
                        prev = s
                        continue
                    if ts >= b:
                        break
                    r = {"start": ts * 1000, "end": (ts + 3600) * 1000}
                    if "sum" in msg["types"]:
                        r["sum"] = s
                    if "change" in msg["types"]:
                        r["change"] = s - prev if prev is not None else s  # HA: prvý riadok bez predchodcu = 0? tu sum
                    rows.append(r)
                    prev = s
            if rows:
                out[sid] = rows
        return out
    if t == "recorder/adjust_sum_statistics":
        a = _ts(msg["start_time"])
        st = state.stats[msg["statistic_id"]]
        for ts in st:
            if ts >= a:
                st[ts] += msg["adjustment"]
        state.adjusts.append(msg)
        return None
    if t == "recorder/import_statistics":
        md = msg["metadata"]
        if "unit_class" in md:
            raise ValueError("extra keys not allowed @ data['metadata']['unit_class']")
        sid = md["statistic_id"]
        if sid not in state.meta:
            state.add_sum(sid)
        for st in msg["stats"]:
            state.stats[sid][_ts(st["start"])] = st["sum"]
        state.imports.append(md)
        return None
    raise ValueError(f"unknown command {t}")


class Handler(socketserver.BaseRequestHandler):
    def recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.request.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def recv_msg(self):
        b1, b2 = self.recv_exact(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self.recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self.recv_exact(8))[0]
        mask = self.recv_exact(4)
        p = self.recv_exact(n)
        if b1 & 0x0F == 8:
            raise ConnectionError
        return bytes(c ^ mask[i % 4] for i, c in enumerate(p)).decode()

    def send_msg(self, obj):
        p = json.dumps(obj).encode()
        n = len(p)
        head = b"\x81" + (bytes([n]) if n < 126 else (b"\x7e" + struct.pack("!H", n) if n < 65536
                                                      else b"\x7f" + struct.pack("!Q", n)))
        self.request.sendall(head + p)

    def handle(self):
        data = b""
        while b"\r\n\r\n" not in data:
            data += self.request.recv(4096)
        key = [l.split(b":", 1)[1].strip() for l in data.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        self.request.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                             b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        self.send_msg({"type": "auth_required"})
        auth = json.loads(self.recv_msg())
        if auth.get("access_token") != "test":
            self.send_msg({"type": "auth_invalid"})
            return
        self.send_msg({"type": "auth_ok", "ha_version": "2026.10.0"})
        try:
            while True:
                msg = json.loads(self.recv_msg())
                try:
                    res = handle(self.server.state, msg)
                    self.send_msg({"id": msg["id"], "type": "result", "success": True, "result": res})
                except ValueError as e:
                    self.send_msg({"id": msg["id"], "type": "result", "success": False,
                                   "error": {"code": "invalid_format", "message": str(e)}})
        except ConnectionError:
            pass


def start(state: State) -> tuple[socketserver.ThreadingTCPServer, int]:
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.state = state
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]
