"""Time handling: parsing source timestamps and choosing the calendar start of a bout.

Individual bout start times are not announced in advance, so a calendar entry starts at the
start of the bout's card (main card, prelims or early prelims). Nothing here invents a time:
when no card start is known, the entry falls back to the event start and finally to an
all-day entry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from .models import Event, Fight

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
# Long enough to be visible in a calendar view, without pretending to know how long a bout lasts.
ENTRY_DURATION = timedelta(hours=1)


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO 8601 timestamp with a UTC offset (e.g. "2026-10-18T00:00Z") into UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        log.warning("Ignoring unparseable timestamp %r", value)
        return None
    if parsed.tzinfo is None:
        log.warning("Ignoring timestamp without UTC offset %r", value)
        return None
    return parsed.astimezone(UTC)


def to_kst(moment: datetime) -> datetime:
    return moment.astimezone(KST)


class TimeSource(StrEnum):
    CARD = "card"  # official card start from UFC
    ESPN_CARD = "espn_card"  # ESPN's start of the card this bout is on
    EVENT = "event"  # event start, used because the card start is unknown
    DATE_ONLY = "date_only"  # no trustworthy start time at all


@dataclass(frozen=True)
class Slot:
    source: TimeSource
    start: datetime | None = None  # in KST
    day: date | None = None  # all-day date, only for DATE_ONLY

    @property
    def end(self) -> datetime | None:
        return self.start + ENTRY_DURATION if self.start else None


def slot_for(event: Event, fight: Fight) -> Slot | None:
    """Pick the calendar start for a bout, from the most to the least specific source."""
    if fight.card_start:
        return Slot(TimeSource.CARD, start=to_kst(fight.card_start))
    if fight.card:
        # Every bout of a card segment shares the segment start.
        sibling = next(
            (f.card_start for f in event.fights if f.card is fight.card and f.card_start), None
        )
        if sibling:
            return Slot(TimeSource.CARD, start=to_kst(sibling))
    if fight.espn_start:
        return Slot(TimeSource.ESPN_CARD, start=to_kst(fight.espn_start))
    event_start = event.start if event.start_reliable else event.espn_start
    if event_start:
        return Slot(TimeSource.EVENT, start=to_kst(event_start))
    if event.local_date:
        return Slot(TimeSource.DATE_ONLY, day=event.local_date)
    return None
