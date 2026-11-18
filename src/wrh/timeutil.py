"""The one timestamp format used in every core table.

Always UTC, always microsecond precision, always a trailing Z, so that
timestamps from different sources sort correctly as plain strings:
2026-11-18T01:12:12.000000Z
"""

from __future__ import annotations

import datetime as dt


def to_iso(t: dt.datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("naive datetime; attach a timezone first")
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_iso() -> str:
    return to_iso(dt.datetime.now(dt.timezone.utc))
