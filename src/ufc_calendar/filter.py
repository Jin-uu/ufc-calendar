"""Decide which bouts go on the calendar: Korean fighters, main events and co-main events."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from .models import Card, Event, EventKind, Fight, Fighter

log = logging.getLogger(__name__)

# Country codes and names used for South Korea by FightMetric ("KOR") and ESPN ("South Korea").
KOREA = {
    "KOR",
    "SKO",
    "SOUTH KOREA",
    "KOREA, SOUTH",
    "KOREA REPUBLIC",
    "REPUBLIC OF KOREA",
    "KOREA, REPUBLIC OF",
}


def is_korean(fighter: Fighter) -> bool | None:
    """True/False from the fighter's nationality; None when the nationality is unknown."""
    if not fighter.country:
        return None
    return fighter.country.strip().upper() in KOREA


@dataclass(frozen=True)
class Selection:
    event: Event
    fight: Fight
    koreans: tuple[Fighter, ...]
    main: bool
    co_main: bool


def headline_flags(event: Event, fight: Fight) -> tuple[bool, bool]:
    """Return (is_main_event, is_co_main_event) from the official bout order.

    FightMetric orders a card from the top: bout 1 is the main event (last bout of the main
    card) and bout 2 the co-main. A position is only trusted when it is unique on the card.
    """
    if fight.order not in (1, 2):
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


def select_fights(events: Iterable[Event]) -> list[Selection]:
    """Bouts with a Korean fighter, plus the main and co-main event of every UFC event.

    Road to UFC and Contender Series events only contribute bouts with Korean fighters.
    """
    selections = []
    for event in events:
        if event.kind is EventKind.OTHER:
            continue
        for fight in event.fights:
            koreans = []
            for fighter in fight.fighters:
                korean = is_korean(fighter)
                if korean is None:
                    log.warning(
                        "%s: nationality unknown for %s (bout %s)",
                        event.name, fighter.name, fight.fight_id,
                    )  # fmt: skip
                elif korean:
                    koreans.append(fighter)
            main = co_main = False
            if event.kind is EventKind.UFC:
                main, co_main = headline_flags(event, fight)
            if koreans or main or co_main:
                selections.append(Selection(event, fight, tuple(koreans), main, co_main))
    return selections
