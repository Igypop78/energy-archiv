"""Načítanie konfigurácie (config.yaml) a tajných údajov (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

DEFAULTS = {
    "timezone": "Europe/Bratislava",
    "database": "/var/lib/energy-archiv/archiv.sqlite",
    "backup": {"dir": "/mnt/nas/energy-archiv", "keep_daily": 14, "keep_monthly": 24},
    # Poradie zdrojov: prvý dostupný vyhráva. Dá sa prepísať pre jednotlivé veličiny.
    "priority": {
        "default": ["oprava", "zsd", "ha", "deye", "manual"],
        "solar": ["oprava", "ha", "deye", "manual"],
    },
    "ha": {"history_start": "2015-01-01", "resync_hours": 48},
    "deye": {
        "base_url": "https://eu1-developer.deyecloud.com",
        "station_id": None,
        # pole Deye -> veličina v archíve (null = nepoužiť)
        "map": {
            "generationPower": "solar",
            "purchasePower": "grid_import",
            "gridPower": "grid_export",
            "chargePower": "battery_in",
            "dischargePower": "battery_out",
        },
        "power_divisor": 1000,  # API vracia W
    },
    "zsd": {"map": {"dodavka": "grid_export", "odber": "grid_import"}},
    "web": {"host": "0.0.0.0", "port": 8765, "password": None},
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    data: dict
    path: Path | None = None
    env: dict = field(default_factory=dict)

    def __getitem__(self, key):
        return self.data[key]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.data["timezone"])

    def priority(self, series: str) -> list[str]:
        p = self.data["priority"]
        base = series.split("_cost")[0].split("_compensation")[0]
        return p.get(series) or p.get(base) or p["default"]


def load(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("ENERGY_ARCHIV_CONFIG", "/etc/energy-archiv/config.yaml"))
    load_dotenv(path.with_name(".env"))
    load_dotenv(Path.cwd() / ".env")
    data = DEFAULTS
    if path.is_file():
        data = _merge(DEFAULTS, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return Config(data=data, path=path if path.is_file() else None, env=dict(os.environ))
