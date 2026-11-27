"""Settings for the scheduled jobs: <data>/config.toml.

A source runs on the schedule only if it has a section here. Passwords and
secret URLs are never in this file; they live in the Keychain (see
``wrh secret set``). See ``docs/config.example.toml`` for every option.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .config import Paths

# How often each job runs when nothing else is configured.
DEFAULT_INTERVAL_MINUTES = {
    "ingest": 15,
    "email": 30,
    "messages": 30,
    "calendar": 360,
    "photos": 720,
    "check": 1440,
}


def path(paths: Paths) -> Path:
    return paths.home / "config.toml"


def load(paths: Paths) -> dict:
    p = path(paths)
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


def section(settings: dict, name: str) -> dict | None:
    s = settings.get(name)
    if s is None or s.get("enabled", True) is False:
        return None
    return s


def interval_minutes(settings: dict, job: str) -> int:
    s = settings.get(job) or {}
    return int(s.get("interval_minutes") or DEFAULT_INTERVAL_MINUTES.get(job, 60))


def stale_after_hours(settings: dict, job: str) -> float:
    """A source with no successful run for this long is reported by `wrh check`."""
    s = settings.get(job) or {}
    if s.get("stale_after_hours"):
        return float(s["stale_after_hours"])
    return max(24.0, 4 * interval_minutes(settings, job) / 60)
