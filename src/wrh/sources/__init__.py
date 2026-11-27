"""Registry of sources. To add one, import it here and add it to SOURCES."""

from __future__ import annotations

from .base import Source
from .browser import BrowserHistory
from .calendar import Calendar
from .email import Email
from .llm import LLMChats
from .messages import Messages
from .photos import Photos

SOURCES: dict[str, Source] = {
    s.name: s for s in [BrowserHistory(), LLMChats(), Email(), Calendar(), Messages(), Photos()]
}


def get_source(name: str) -> Source:
    try:
        return SOURCES[name]
    except KeyError:
        raise SystemExit(f"Unknown source {name!r}. Known: {', '.join(sorted(SOURCES))}")
