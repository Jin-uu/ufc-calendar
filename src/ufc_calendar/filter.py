"""Which bouts appear in which feed.

Every bout of the collected events becomes one master entry (see state.py). A feed is a
combination of settings chosen on the website; each published .ics file is the master entries
that match one feed:

- country:  bouts with a fighter of this nationality (ISO alpha-2), or None
- gender:   "all", "men" or "women"
- headline: "none", "main", "comain" or "both" — main/co-main events of UFC events
- side:     also take country bouts from Road to UFC and Dana White's Contender Series
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from .models import Card, Event, EventKind, Fight

log = logging.getLogger(__name__)

GENDERS = ("all", "men", "women")
HEADLINES = ("none", "main", "comain", "both")
DEFAULT_COUNTRY = "KR"


@dataclass(frozen=True)
class Feed:
    country: str | None = DEFAULT_COUNTRY
    gender: str = "all"
    headline: str = "both"
    side: bool = True

    @property
    def slug(self) -> str:
        side = "rtu" if self.country and self.side else "ufc"
        return f"{(self.country or 'all').lower()}-{self.gender}-{self.headline}-{side}"


DEFAULT_FEED = Feed()  # published as ufc.ics as well


def all_feeds(countries: Iterable[str]) -> list[Feed]:
    """Every combination offered on the website (a feed must select something)."""
    feeds = [Feed(None, g, h, False) for g in GENDERS for h in HEADLINES if h != "none"]
    for country in sorted(set(countries)):
        feeds += [Feed(country, g, h, s) for g in GENDERS for h in HEADLINES for s in (True, False)]
    return feeds


class Matchable(Protocol):
    kind: str
    main: bool
    co_main: bool
    women: bool
    countries: list[str]


def matches(feed: Feed, bout: Matchable) -> bool:
    if (feed.gender == "men" and bout.women) or (feed.gender == "women" and not bout.women):
        return False
    by_country = bool(
        feed.country
        and feed.country in bout.countries
        and (bout.kind == EventKind.UFC or feed.side)
    )
    by_headline = bout.kind == EventKind.UFC and (
        (feed.headline in ("main", "both") and bout.main)
        or (feed.headline in ("comain", "both") and bout.co_main)
    )
    return by_country or by_headline


def headline_flags(event: Event, fight: Fight) -> tuple[bool, bool]:
    """Return (is_main_event, is_co_main_event) from the official bout order.

    FightMetric orders a card from the top: bout 1 is the main event (last bout of the main
    card) and bout 2 the co-main. A position is only trusted when it is unique on the card.
    """
    if event.kind is not EventKind.UFC or fight.order not in (1, 2):
        return False, False
    if sum(1 for f in event.fights if f.order == fight.order) != 1:
        log.warning(
            "%s: bout order %s is not unique, skipping main/co-main", event.name, fight.order
        )
        return False, False
    if fight.order == 1:
        return fight.card in (Card.MAIN, None), False
    return False, _on_main_card(event, fight)


def _on_main_card(event: Event, fight: Fight) -> bool:
    if fight.card is not None:
        return fight.card is Card.MAIN
    # The UFC feed has not assigned the bout to a card yet. Accept ESPN's evidence when ESPN
    # already splits the event into several card times and this bout starts with the main event.
    main_event = next((f for f in event.fights if f.order == 1), None)
    espn_times = {f.espn_start for f in event.fights if f.espn_start}
    return bool(
        len(espn_times) >= 2
        and main_event
        and fight.espn_start
        and fight.espn_start == main_event.espn_start
    )


@dataclass(frozen=True)
class Bout:
    event: Event
    fight: Fight
    main: bool
    co_main: bool

    @property
    def kind(self) -> str:
        return self.event.kind

    @property
    def women(self) -> bool:
        return self.fight.women

    @property
    def countries(self) -> list[str]:
        return sorted({f.country for f in self.fight.fighters if f.country})


def collect_bouts(events: Iterable[Event]) -> list[Bout]:
    """All bouts of UFC, Road to UFC and Contender Series events, with headline flags."""
    bouts = []
    for event in events:
        if event.kind is EventKind.OTHER:
            continue
        for fight in event.fights:
            for fighter in fight.fighters:
                if fighter.country is None:
                    log.warning(
                        "%s: nationality unknown for %s (bout %s)",
                        event.name, fighter.name, fight.fight_id,
                    )  # fmt: skip
            bouts.append(Bout(event, fight, *headline_flags(event, fight)))
    return bouts
