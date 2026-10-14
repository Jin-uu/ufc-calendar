"""Render bouts as calendar entries and write/validate iCalendar (RFC 5545) files."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, timedelta

from icalendar import Calendar
from icalendar import Event as VEvent

from .countries import country_name, flag
from .filter import Bout, Feed, matches
from .models import EventKind
from .schedule import Slot, TimeSource, slot_for, to_kst
from .state import CANCELLED, Draft, Entry

log = logging.getLogger(__name__)

PRODID = "-//ufc-calendar//UFC Fights//KO"
REFRESH_INTERVAL = timedelta(hours=6)
MAX_LINE_OCTETS = 75
CANCELLED_PREFIX = "❌ 취소 | "

EVENT_LABELS = {
    EventKind.UFC: "UFC",
    EventKind.ROAD_TO_UFC: "Road to UFC",
    EventKind.CONTENDER_SERIES: "DWCS",
}
HEADLINE_NAMES = {"main": "메인", "comain": "코메인", "both": "메인·코메인"}
GENDER_NAMES = {"men": "남자부", "women": "여자부"}


class CalendarValidationError(ValueError):
    """The generated calendar is not safe to publish."""


def _time_note(bout: Bout, slot: Slot) -> str:
    card = bout.fight.card
    if slot.source is TimeSource.CARD:
        name = card.label_ko if card else "카드"
        return f"※ 표시된 시간은 {name} 시작 시간이며,\n실제 경기 시작 시간은 다를 수 있습니다."
    if slot.source is TimeSource.ESPN_CARD:
        return (
            "※ 표시된 시간은 ESPN에 공개된 이 경기의 카드 시작 시간입니다.\n"
            "(UFC 공식 카드 시간 미발표) 실제 경기 시작 시간은 다를 수 있습니다."
        )
    if slot.source is TimeSource.EVENT:
        return (
            "※ 카드 시작 시간이 아직 발표되지 않아 대회 시작 시간을 대신 표시했습니다.\n"
            "실제 경기 시작 시간은 다를 수 있습니다."
        )
    return "※ 시작 시간이 아직 발표되지 않아 날짜(현지 기준)만 표시했습니다."


def description_for(bout: Bout, slot: Slot) -> str:
    event, fight = bout.event, bout.fight
    kind = "Main Event" if bout.main else "Co-Main Event" if bout.co_main else "Bout"
    division = fight.weight_class or "TBD"
    if fight.title:
        division += f" ({fight.title})"
    fighters = ", ".join(
        f"{f.name} ({country_name(f.country) if f.country else 'unknown'})" for f in fight.fighters
    )
    lines = [
        event.name,
        "",
        f"Fight: {fight.matchup}",
        f"Fighters: {fighters or 'TBD'}",
        f"Division: {division}",
        f"Card: {fight.card.label if fight.card else 'TBD'}",
        f"Type: {kind}",
    ]
    if event.venue:
        lines.append(f"Venue: {event.venue}")
    lines += ["", _time_note(bout, slot), "", "Official:", event.url]
    return "\n".join(lines)


def draft_for(bout: Bout) -> Draft | None:
    slot = slot_for(bout.event, bout.fight)
    if slot is None:
        log.warning("%s: no date for bout %s, skipping", bout.event.name, bout.fight.fight_id)
        return None
    return Draft(
        event_id=bout.event.event_id,
        fight_id=bout.fight.fight_id,
        fighter_ids=tuple(f.fighter_id for f in bout.fight.fighters if f.fighter_id is not None),
        matchup=bout.fight.matchup,
        kind=str(bout.event.kind),
        main=bout.main,
        co_main=bout.co_main,
        women=bout.fight.women,
        countries=tuple(bout.countries),
        description=description_for(bout, slot),
        url=bout.event.url,
        start=slot.start.isoformat() if slot.start else None,
        end=slot.end.isoformat() if slot.end else None,
        day=slot.day.isoformat() if slot.day else None,
    )


def summary_for(entry: Entry, feed: Feed) -> str:
    label = EVENT_LABELS.get(EventKind(entry.kind), "UFC")
    if entry.main:
        label += " Main"
    elif entry.co_main:
        label += " Co-Main"
    icon = flag(feed.country) if feed.country and feed.country in entry.countries else "🥊"
    prefix = CANCELLED_PREFIX if entry.status == CANCELLED else ""
    return f"{prefix}{icon} {label} | {entry.matchup}"


def feed_entries(entries: Iterable[Entry], feed: Feed) -> list[Entry]:
    """The master entries shown in ``feed``.

    The cancelled copy of a bout whose fighter changed is shown only where the current
    version of the bout no longer appears (e.g. a country feed after its fighter was replaced).
    """
    entries = list(entries)
    by_uid = {e.uid: e for e in entries}
    selected = []
    for entry in entries:
        if not matches(feed, entry):
            continue
        current = by_uid.get(entry.replaced_by) if entry.replaced_by else None
        if current is not None and matches(feed, current):
            continue
        selected.append(entry)
    return selected


def calendar_name(feed: Feed) -> str:
    parts = []
    if feed.country:
        parts.append(f"{flag(feed.country)} {country_name(feed.country, korean=True)}")
    if feed.headline != "none":
        parts.append(HEADLINE_NAMES[feed.headline])
    name = "UFC " + " + ".join(parts)
    if feed.gender in GENDER_NAMES:
        name += f" · {GENDER_NAMES[feed.gender]}"
    return name


def _vevent(entry: Entry, feed: Feed) -> VEvent:
    event = VEvent()
    stamp = datetime.fromisoformat(entry.last_modified)
    event.add("uid", entry.uid)
    event.add("dtstamp", stamp)
    event.add("last-modified", stamp)
    event.add("sequence", entry.sequence)
    if entry.start and entry.end:
        event.add("dtstart", to_kst(datetime.fromisoformat(entry.start)))
        event.add("dtend", to_kst(datetime.fromisoformat(entry.end)))
    else:
        day = date.fromisoformat(entry.day)
        event.add("dtstart", day)
        event.add("dtend", day + timedelta(days=1))
    event.add("summary", summary_for(entry, feed))
    event.add("description", entry.description)
    event.add("url", entry.url)
    event.add("status", entry.status)
    event.add("transp", "TRANSPARENT")
    return event


def build_calendar(entries: Iterable[Entry], feed: Feed) -> bytes:
    """The .ics file of ``feed`` built from the master entries."""
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", calendar_name(feed))
    calendar.add("x-wr-caldesc", "UFC 경기 일정 (매일 자동 갱신)")
    calendar.add("x-wr-timezone", "Asia/Seoul")
    calendar.add("refresh-interval", REFRESH_INTERVAL, parameters={"VALUE": "DURATION"})
    calendar.add("x-published-ttl", "PT6H")
    for entry in sorted(feed_entries(entries, feed), key=lambda e: (e.begins, e.uid)):
        calendar.add_component(_vevent(entry, feed))
    calendar.add_missing_timezones()
    return calendar.to_ical()


def validate_calendar(data: bytes) -> int:
    """Parse the generated file back and check what Apple Calendar relies on.

    Returns the number of events. Raises CalendarValidationError on any problem.
    """
    try:
        text = data.decode("utf-8")
        calendar = Calendar.from_ical(data)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CalendarValidationError(f"calendar does not parse: {exc}") from exc
    if calendar.name != "VCALENDAR" or str(calendar.get("VERSION")) != "2.0":
        raise CalendarValidationError("missing VCALENDAR/VERSION:2.0")
    if not calendar.get("PRODID"):
        raise CalendarValidationError("missing PRODID")
    for line in text.split("\r\n"):
        if len(line.encode("utf-8")) > MAX_LINE_OCTETS:
            raise CalendarValidationError(f"unfolded line longer than 75 octets: {line[:40]!r}")
    uids = set()
    events = calendar.walk("VEVENT")
    for event in events:
        for prop in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY"):
            if prop not in event:
                raise CalendarValidationError(f"event without {prop}: {event.get('UID')}")
        uid = str(event["UID"])
        if uid in uids:
            raise CalendarValidationError(f"duplicate UID {uid}")
        uids.add(uid)
        if event.decoded("DTEND") <= event.decoded("DTSTART"):
            raise CalendarValidationError(f"event {uid} ends before it starts")
    timed = any(isinstance(e.decoded("DTSTART"), datetime) for e in events)
    if timed and not calendar.walk("VTIMEZONE"):
        raise CalendarValidationError("timed events without VTIMEZONE")
    return len(events)
