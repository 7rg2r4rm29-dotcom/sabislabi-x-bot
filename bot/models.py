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
    appeal: float = 0.0      # intérêt hors fraîcheur (poids du flux × engagement × public), calculé par NewsHub
    body: str = ""           # texte de l'article, lu seulement pour les histoires retenues

    def brief(self) -> dict:
        return {"title": self.title, "url": self.url, "source": self.source}


@dataclass
class Visual:
    kind: str                # "photo" (la photo de l'actu citée) | "ai" ; « shot » : ancien nom
    target: str              # URL de la page de l'actu (photo) ou description de photo (ai)


@dataclass
class Draft:
    kind: str                # gm | tip | news | thread | reactive
    texts: list[str]
    visual: Visual | None = None
    poll: dict | None = None
    sources: list[NewsItem] = field(default_factory=list)
    reason: str = ""
    angle: str = ""
    hook: str = ""           # forme d'accroche du post (writer.HOOK_SHAPES), pour apprendre ce qui marche
    from_fallback: bool = False
    used_notes: list[str] = field(default_factory=list)   # notes perso (/note) utilisées
    # Renseignés par le planificateur quand l'image est prête (🔄 Autre version) :
    # la photo est celle de l'actu du NOUVEAU texte, pas celle de l'ancien.
    image_path: str | None = None
    image_kind: str | None = None        # "photo" | "ai" ("shot" : capture, avant le 14/11/2026)
