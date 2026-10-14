"""Collect UFC events and enrich them with ESPN data.

Primary source: the UFC's FightMetric live-data feed (the JSON that ufc.com and the UFC app
read). It is free, needs no key and provides fight ids, bout order, card segments, card start
times and weight classes. It has no event listing, so events are found by walking event ids.

Secondary source: ESPN's public scoreboard JSON. It provides each fighter's country flag
(the nationality ufc.com displays) and card start times for bouts that the UFC feed has not
assigned to a card yet.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import Card, Event, EventKind, Fight, Fighter
from .schedule import parse_timestamp

log = logging.getLogger(__name__)

FIGHTMETRIC_EVENT_URL = "https://d29dxerjsp82wz.cloudfront.net/api/v3/event/live/{event_id}.json"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
UFC_EVENT_URL = "https://www.ufc.com/event/{slug}"
UFC_EVENTS_URL = "https://www.ufc.com/events"

USER_AGENT = "ufc-calendar/0.1 (+https://github.com/Jin-uu/ufc-calendar)"
TIMEOUT = (5, 20)  # connect, read (seconds)

CARD_SEGMENTS = {"Main": Card.MAIN, "Prelims1": Card.PRELIMS, "Prelims2": Card.EARLY_PRELIMS}
MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip
US_EASTERN = ZoneInfo("America/New_York")
ESPN_MATCH_WINDOW = timedelta(days=2)


class FetchError(Exception):
    """A data source could not be reached or returned an unusable response."""


def make_session() -> requests.Session:
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_json(session: requests.Session, url: str, params: Mapping[str, Any] | None = None) -> Any:
    try:
        response = session.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise FetchError(f"GET {url} failed: {exc}") from exc
    if response.status_code != 200:
        raise FetchError(f"GET {url} returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(f"GET {url} returned invalid JSON") from exc


# --- FightMetric -------------------------------------------------------------------------


def fetch_event_detail(session: requests.Session, event_id: int) -> dict | None:
    """Return the raw FightMetric event, or None when no event uses this id."""
    payload = fetch_json(session, FIGHTMETRIC_EVENT_URL.format(event_id=event_id))
    detail = payload.get("LiveEventDetail") if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        raise FetchError(f"Unexpected FightMetric payload for event {event_id}")
    return detail or None


@dataclass
class ScanResult:
    raw_events: dict[int, dict] = field(default_factory=dict)
    stale_ids: list[int] = field(default_factory=list)  # served from the previous snapshot
    highest_id: int | None = None


def scan_events(
    session: requests.Session,
    start_id: int,
    *,
    known_max_id: int | None = None,
    previous_raw: Mapping[int, dict] | None = None,
    max_misses: int = 10,
    max_requests: int = 200,
    delay: float = 0.25,
) -> ScanResult:
    """Walk FightMetric event ids upwards from ``start_id``.

    Ids are allocated when an event is announced, so every upcoming event has an id at or
    above the smallest id that was upcoming on the previous run. The walk stops after
    ``max_misses`` consecutive unused ids beyond the highest known id.

    If a request fails for an event that the previous run knew, its previous snapshot is used.
    A failure on any other id raises FetchError, because a silently skipped id could hide an
    event and turn into wrongly cancelled entries.
    """
    previous_raw = previous_raw or {}
    result = ScanResult(highest_id=known_max_id)
    misses = 0
    event_id = start_id
    for attempt in range(max_requests):
        if attempt and delay:
            time.sleep(delay)
        try:
            detail = fetch_event_detail(session, event_id)
        except FetchError as exc:
            if event_id not in previous_raw:
                raise
            log.warning("Event %s: %s; using the snapshot from the previous run", event_id, exc)
            detail = previous_raw[event_id]
            result.stale_ids.append(event_id)
        if detail is None:
            misses += 1
            if misses >= max_misses and (result.highest_id is None or event_id > result.highest_id):
                break
        else:
            misses = 0
            result.raw_events[event_id] = detail
            result.highest_id = max(result.highest_id or event_id, event_id)
        event_id += 1
    else:
        log.warning("Stopped scanning FightMetric after %d requests", max_requests)
    log.info(
        "Scanned FightMetric event ids %d-%d: %d events", start_id, event_id, len(result.raw_events)
    )
    return result


def event_kind(name: str, organization: str | None) -> EventKind:
    org = (organization or "").lower()
    lowered = name.lower()
    if "road to ufc" in org or lowered.startswith("road to ufc"):
        return EventKind.ROAD_TO_UFC
    if "contender series" in org or lowered.startswith(("dwcs", "dana white's contender")):
        return EventKind.CONTENDER_SERIES
    if org == "ultimate fighting championship":
        return EventKind.UFC
    return EventKind.OTHER


def event_url(kind: EventKind, name: str, local_date: date | None) -> str:
    """Build the ufc.com event page URL (ufc.com/event/ufc-333 or .../ufc-fight-night-<date>)."""
    if kind is EventKind.UFC:
        numbered = re.match(r"UFC (\d+)\b", name)
        if numbered:
            return UFC_EVENT_URL.format(slug=f"ufc-{numbered.group(1)}")
        if local_date:
            slug = f"ufc-fight-night-{MONTHS[local_date.month - 1]}-{local_date:%d-%Y}"
            return UFC_EVENT_URL.format(slug=slug)
    return UFC_EVENTS_URL


def _parse_gmt_offset(value: object) -> timedelta | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"GMT([+-])(\d{1,2}):?(\d{2})?", value.strip())
    if not match:
        return None
    sign = 1 if match.group(1) == "+" else -1
    return sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))


def parse_event(raw: Mapping[str, Any]) -> Event:
    """Convert a FightMetric LiveEventDetail into an Event. Raises ValueError if unusable."""
    event_id = raw.get("EventId")
    name = raw.get("Name")
    if not isinstance(event_id, int) or not isinstance(name, str) or not name.strip():
        raise ValueError(f"event without id or name: {event_id!r}")
    name = name.strip()
    start = parse_timestamp(raw.get("StartTime"))
    offset = _parse_gmt_offset(raw.get("TimeZone"))
    local_date = None
    if start and offset is not None:
        local_date = (start + offset).date()
    elif start:
        local_date = start.astimezone(US_EASTERN).date()
    location = raw.get("Location") or {}
    venue = ", ".join(
        p for p in (location.get("Venue"), location.get("City"), location.get("Country")) if p
    )
    kind = event_kind(name, (raw.get("Organization") or {}).get("Name"))
    event = Event(
        event_id=event_id,
        name=name,
        kind=kind,
        url=event_url(kind, name, local_date),
        start=start,
        start_reliable=start is not None and offset is not None,
        local_date=local_date,
        venue=venue or None,
        status=raw.get("Status"),
    )
    for raw_fight in raw.get("FightCard") or []:
        fight = parse_fight(raw_fight, event_name=name)
        if fight:
            event.fights.append(fight)
    return event


def parse_fight(raw: Mapping[str, Any], event_name: str = "") -> Fight | None:
    fight_id = raw.get("FightId")
    raw_fighters = [f for f in raw.get("Fighters") or [] if isinstance(f, dict)]
    if not isinstance(fight_id, int):
        log.warning("%s: skipping a bout without FightId", event_name)
        return None
    raw_fighters.sort(key=lambda f: 0 if f.get("Corner") == "Red" else 1)
    fighters = [f for f in map(parse_fighter, raw_fighters) if f]
    if len(fighters) < 2:
        log.info("%s: bout %s has no confirmed opponent yet", event_name, fight_id)

    segment = raw.get("CardSegment")
    card = CARD_SEGMENTS.get(segment) if segment else None
    if segment and card is None:
        log.warning("%s: unknown card segment %r for bout %s", event_name, segment, fight_id)
    order = raw.get("FightOrder")

    weight = raw.get("WeightClass") or {}
    weight_class = weight.get("Description")
    if weight.get("CatchWeight"):
        weight_class = f"Catchweight ({weight['CatchWeight']} lb)"
    title = next(
        (
            a["Name"]
            for a in raw.get("Accolades") or []
            if "title" in str(a.get("Name", "")).lower()
        ),
        None,
    )
    return Fight(
        fight_id=fight_id,
        fighters=fighters,
        order=order if isinstance(order, int) else None,
        card=card,
        card_start=parse_timestamp(raw.get("CardSegmentStartTime")),
        weight_class=weight_class,
        title=title,
        status=raw.get("Status"),
    )


_SNAPSHOT_KEYS = {
    "event": ("EventId", "Name", "StartTime", "TimeZone", "Status", "Organization", "Location"),
    "fight": ("FightId", "FightOrder", "Status", "CardSegment", "CardSegmentStartTime",
              "WeightClass", "Accolades"),
    "fighter": ("FighterId", "Name", "Born", "Corner"),
}  # fmt: skip


def snapshot_event(raw: Mapping[str, Any]) -> dict:
    """Keep only what parse_event reads, so the next run can fall back to it."""
    snapshot = {k: raw.get(k) for k in _SNAPSHOT_KEYS["event"]}
    snapshot["FightCard"] = [
        {
            **{k: fight.get(k) for k in _SNAPSHOT_KEYS["fight"]},
            "Fighters": [
                {k: f.get(k) for k in _SNAPSHOT_KEYS["fighter"]}
                for f in fight.get("Fighters") or []
            ],
        }
        for fight in raw.get("FightCard") or []
    ]
    return snapshot


def parse_fighter(raw: Mapping[str, Any]) -> Fighter | None:
    parts = raw.get("Name") or {}
    name = " ".join(
        str(p).strip() for p in (parts.get("FirstName"), parts.get("LastName")) if p
    ).strip()
    if not name:
        return None
    born = (raw.get("Born") or {}).get("TriCode")
    fighter_id = raw.get("FighterId")
    return Fighter(
        name=name,
        fighter_id=fighter_id if isinstance(fighter_id, int) else None,
        born_country_code=born.strip().upper() if isinstance(born, str) and born.strip() else None,
    )


# --- ESPN ----------------------------------------------------------------------------------


@dataclass
class EspnBout:
    competitors: list[tuple[str, str | None]]  # (name, country)
    start: datetime | None = None  # start of this bout's card, if ESPN marks it valid
    event_start: datetime | None = None


def fetch_espn_bouts(session: requests.Session, first_day: date, last_day: date) -> list[EspnBout]:
    params = {"dates": f"{first_day:%Y%m%d}-{last_day:%Y%m%d}", "limit": 200}
    payload = fetch_json(session, ESPN_SCOREBOARD_URL, params=params)
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise FetchError("Unexpected ESPN scoreboard payload")
    bouts = []
    for event in events:
        event_start = parse_timestamp(event.get("date"))
        for competition in event.get("competitions") or []:
            competitors = []
            for competitor in competition.get("competitors") or []:
                athlete = competitor.get("athlete") or {}
                name = athlete.get("displayName") or athlete.get("fullName")
                if name:
                    competitors.append((name, espn_country(athlete.get("flag"))))
            start = (
                parse_timestamp(competition.get("date")) if competition.get("timeValid") else None
            )
            bouts.append(EspnBout(competitors, start, event_start or start))
    log.info("ESPN: %d events, %d bouts", len(events), len(bouts))
    return bouts


def espn_country(flag: object) -> str | None:
    if not isinstance(flag, dict):
        return None
    alt = str(flag.get("alt") or "").strip()
    href = str(flag.get("href") or "")
    if not alt or alt.lower() == "default" or href.endswith("/blank.png"):
        return None
    return alt


def name_keys(name: str) -> set[str]:
    """Comparable forms of a name: accents, case and spacing ignored, and token order ignored."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    if not tokens:
        return set()
    return {"".join(tokens), " ".join(sorted(tokens))}


def _match_bout(
    fight: Fight, event: Event, bouts: Iterable[EspnBout]
) -> tuple[EspnBout, dict[int, int]] | None:
    """Find the ESPN bout for a fight; returns it with fighter index -> competitor index."""
    if not event.start:
        return None
    fighter_keys = [name_keys(f.name) for f in fight.fighters]
    for bout in bouts:
        if not bout.event_start or abs(bout.event_start - event.start) > ESPN_MATCH_WINDOW:
            continue
        mapping = {
            i: j
            for i, keys in enumerate(fighter_keys)
            for j, (name, _) in enumerate(bout.competitors)
            if keys & name_keys(name)
        }
        if mapping:
            return bout, mapping
    return None


def apply_espn(
    events: Iterable[Event], bouts: list[EspnBout], cache: Mapping[str, Mapping[str, str]]
) -> dict[str, dict[str, str]]:
    """Attach nationalities and ESPN card times to the fights of ``events``.

    A fighter's nationality comes from ESPN when ESPN lists them, otherwise from the value
    ESPN gave on an earlier run (``cache``), otherwise from the FightMetric birthplace.
    Returns the updated cache, limited to the fighters seen in ``events``.
    """
    new_cache: dict[str, dict[str, str]] = {}
    for event in events:
        for fight in event.fights:
            espn_countries: dict[int, str | None] = {}
            match = _match_bout(fight, event, bouts)
            if match:
                bout, mapping = match
                fight.espn_start = bout.start
                event.espn_start = event.espn_start or bout.event_start
                espn_countries = {i: bout.competitors[j][1] for i, j in mapping.items()}
            for i, fighter in enumerate(fight.fighters):
                espn = espn_countries.get(i)
                key = str(fighter.fighter_id) if fighter.fighter_id is not None else None
                cached = cache.get(key, {}).get("country") if key else None
                if espn:
                    fighter.country, fighter.country_source = espn, "espn"
                elif cached:
                    fighter.country, fighter.country_source = cached, "espn-cache"
                elif fighter.born_country_code:
                    fighter.country, fighter.country_source = (
                        fighter.born_country_code,
                        "fightmetric-born",
                    )
                if key and fighter.country_source in ("espn", "espn-cache"):
                    new_cache[key] = {"name": fighter.name, "country": fighter.country}
    return new_cache
