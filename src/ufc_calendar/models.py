"""Data models shared by the scraper, filter and calendar modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class Card(StrEnum):
    MAIN = "main"
    PRELIMS = "prelims"
    EARLY_PRELIMS = "early_prelims"

    @property
    def label(self) -> str:
        return _CARD_LABELS[self][0]

    @property
    def label_ko(self) -> str:
        return _CARD_LABELS[self][1]


_CARD_LABELS = {
    Card.MAIN: ("Main Card", "메인 카드"),
    Card.PRELIMS: ("Prelims", "프릴림 카드"),
    Card.EARLY_PRELIMS: ("Early Prelims", "얼리 프릴림"),
}


class EventKind(StrEnum):
    UFC = "ufc"  # numbered events, Fight Nights, Noche UFC, ...
    ROAD_TO_UFC = "road_to_ufc"
    CONTENDER_SERIES = "contender_series"
    OTHER = "other"


@dataclass
class Fighter:
    name: str
    fighter_id: int | None = None
    born_country_code: str | None = None  # FightMetric birthplace, e.g. "KOR"
    country: str | None = None  # resolved nationality, ISO alpha-2 (e.g. "KR")
    country_source: str | None = None  # "espn", "espn-cache" or "fightmetric-born"
    divisions: tuple[str, ...] = ()  # the fighter's weight classes, e.g. ("Women's Flyweight",)


@dataclass
class Fight:
    fight_id: int
    fighters: list[Fighter]  # red corner first
    order: int | None = None  # official bout order, 1 = main event
    card: Card | None = None
    card_start: datetime | None = None  # official start of the bout's card segment (UTC)
    weight_class: str | None = None
    title: str | None = None  # e.g. "UFC Featherweight Title"
    status: str | None = None
    espn_start: datetime | None = None  # start of the bout's card according to ESPN (UTC)

    @property
    def women(self) -> bool:
        """A women's bout: women's division, or (catchweight) fighters from women's divisions."""
        if self.weight_class and self.weight_class.lower().startswith("women"):
            return True
        return any(d.lower().startswith("women") for f in self.fighters for d in f.divisions)

    @property
    def matchup(self) -> str:
        names = [f.name for f in self.fighters[:2]]
        names += ["TBD"] * (2 - len(names))
        return " vs ".join(names)


@dataclass
class Event:
    event_id: int
    name: str
    kind: EventKind
    url: str
    start: datetime | None = None  # FightMetric event start (UTC)
    start_reliable: bool = False  # False when FightMetric has no local time zone for the event
    local_date: date | None = None  # event date at the venue
    venue: str | None = None
    status: str | None = None
    fights: list[Fight] = field(default_factory=list)
    espn_start: datetime | None = None  # event start according to ESPN (UTC)
