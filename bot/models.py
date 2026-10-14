"""Structures de données partagées entre les modules."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


def stable_id(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:12]


@dataclass
class NewsItem:
    id: str
    title: str
    summary: str
    url: str
    source: str
    published: datetime | None
    official: bool = False
    kind: str = "rss"        # rss | hn | x
    engagement: int = 0      # points HN ou likes X
    score: float = 0.0

    def brief(self) -> dict:
        return {"title": self.title, "url": self.url, "source": self.source}


@dataclass
class Visual:
    kind: str                # "screenshot" | "ai"
    target: str              # URL (screenshot) ou description de photo (ai)


@dataclass
class Draft:
    kind: str                # gm | tip | news | thread | reactive
    texts: list[str]
    visual: Visual | None = None
    poll: dict | None = None
    sources: list[NewsItem] = field(default_factory=list)
    reason: str = ""
    angle: str = ""
    from_fallback: bool = False
    used_notes: list[str] = field(default_factory=list)   # notes perso (/note) utilisées
