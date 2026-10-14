"""Test helpers: fixture loading, a fake HTTP session and builders for synthetic data."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ufc_calendar.models import Card, Event, EventKind, Fight, Fighter
from ufc_calendar.scraper import ESPN_SCOREBOARD_URL, FIGHTMETRIC_EVENT_URL

FIXTURES = Path(__file__).parent / "fixtures"
EMPTY_EVENT = {"LiveEventDetail": {}}


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fm_url(event_id: int) -> str:
    return FIGHTMETRIC_EVENT_URL.format(event_id=event_id)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, body: bytes | None = None):
        self.status_code = status_code
        self._payload = payload
        self.content = body if body is not None else json.dumps(payload).encode()

    def json(self) -> Any:
        return json.loads(self.content)


class FakeSession:
    """Serves canned responses by URL.

    A route value may be a JSON payload, a FakeResponse or an exception to raise. Unknown
    FightMetric URLs answer like an unused event id; other unknown URLs answer 404.
    """

    def __init__(self, routes: dict[str, Any] | None = None):
        self.routes = dict(routes or {})
        self.calls: list[str] = []

    def get(self, url: str, params: Any = None, timeout: Any = None) -> FakeResponse:
        self.calls.append(url)
        if url in self.routes:
            value = self.routes[url]
        elif url.startswith(FIGHTMETRIC_EVENT_URL.split("{")[0]):
            value = EMPTY_EVENT
        else:
            value = FakeResponse(404, {"message": "not found"})
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, FakeResponse) else FakeResponse(200, value)


def fixture_session(*event_ids: int, espn: bool = True, **extra: Any) -> FakeSession:
    routes = {fm_url(i): load_fixture(f"fightmetric_{i}.json") for i in event_ids}
    if espn:
        routes[ESPN_SCOREBOARD_URL] = load_fixture("espn_scoreboard.json")
    routes.update(extra)
    return FakeSession(routes)


# --- raw FightMetric builders -------------------------------------------------------------


def raw_fighter(fighter_id, first, last, born="USA", corner="Red") -> dict:
    return {
        "FighterId": fighter_id,
        "Name": {"FirstName": first, "LastName": last},
        "Born": {"TriCode": born} if born else None,
        "Corner": corner,
    }


def raw_fight(fight_id, order, segment, start, red, blue, weight="Lightweight") -> dict:
    return {
        "FightId": fight_id,
        "FightOrder": order,
        "Status": "Upcoming",
        "CardSegment": segment,
        "CardSegmentStartTime": start,
        "WeightClass": {"Description": weight} if weight else None,
        "Accolades": [],
        "Fighters": [red, {**blue, "Corner": "Blue"}],
    }


def raw_event(event_id=2000, fights=(), name="UFC Fight Night: Alpha vs. Bravo",
              start="2026-11-07T22:00Z", tz="GMT-08:00",
              org="Ultimate Fighting Championship") -> dict:  # fmt: skip
    return {
        "EventId": event_id,
        "Name": name,
        "StartTime": start,
        "TimeZone": tz,
        "Status": "Upcoming",
        "Organization": {"Name": org},
        "Location": {"Venue": "Meta Apex", "City": "Las Vegas", "Country": "USA"},
        "FightCard": list(fights),
    }


# --- model builders ------------------------------------------------------------------------


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def fighter(name: str, fighter_id: int | None = None, country: str | None = "USA") -> Fighter:
    return Fighter(name=name, fighter_id=fighter_id, country=country, country_source="test")


def fight(fight_id: int, order: int | None, card: Card | None, *fighters: Fighter,
          card_start: datetime | None = None, espn_start: datetime | None = None,
          weight_class: str | None = "Lightweight") -> Fight:  # fmt: skip
    return Fight(
        fight_id=fight_id,
        fighters=list(fighters),
        order=order,
        card=card,
        card_start=card_start,
        espn_start=espn_start,
        weight_class=weight_class,
    )


def event(*fights: Fight, kind: EventKind = EventKind.UFC, event_id: int = 2000,
          start: datetime | None = None, start_reliable: bool = True,
          name: str = "UFC Fight Night: Alpha vs. Bravo") -> Event:  # fmt: skip
    start = start or utc(2026, 11, 7, 22)
    return Event(
        event_id=event_id,
        name=name,
        kind=kind,
        url="https://www.ufc.com/event/ufc-fight-night-november-07-2026",
        start=start,
        start_reliable=start_reliable,
        local_date=date(2026, 11, 7),
        venue="Meta Apex, Las Vegas, USA",
        fights=list(fights),
    )
