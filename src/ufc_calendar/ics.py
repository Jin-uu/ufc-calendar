"""Render selected bouts as calendar entries and write/validate the iCalendar (RFC 5545) file."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, timedelta

from icalendar import Calendar
from icalendar import Event as VEvent

from .filter import Selection
from .models import EventKind
from .schedule import Slot, TimeSource, slot_for, to_kst
from .state import Draft, Entry

log = logging.getLogger(__name__)

PRODID = "-//ufc-calendar//UFC Korean & Headline Fights//KO"
CALENDAR_NAME = "UFC 주요 경기"
CALENDAR_DESCRIPTION = "한국 선수 출전 경기와 UFC 메인·코메인 이벤트 (매일 자동 갱신)"
REFRESH_INTERVAL = timedelta(hours=6)
MAX_LINE_OCTETS = 75

EVENT_LABELS = {
    EventKind.UFC: "UFC",
    EventKind.ROAD_TO_UFC: "Road to UFC",
    EventKind.CONTENDER_SERIES: "DWCS",
}


class CalendarValidationError(ValueError):
    """The generated calendar is not safe to publish."""


def summary_for(selection: Selection) -> str:
    label = EVENT_LABELS.get(selection.event.kind, "UFC")
    if selection.main:
        label += " Main"
    elif selection.co_main:
        label += " Co-Main"
    icon = "🇰🇷" if selection.koreans else "🥊"
    return f"{icon} {label} | {selection.fight.matchup}"


def _time_note(selection: Selection, slot: Slot) -> str:
    card = selection.fight.card
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


def description_for(selection: Selection, slot: Slot) -> str:
    event, fight = selection.event, selection.fight
    types = [
        label
        for label, flag in (
            ("Main Event", selection.main),
            ("Co-Main Event", selection.co_main),
            ("Korean Fighter", bool(selection.koreans)),
        )
        if flag
    ]
    division = fight.weight_class or "TBD"
    if fight.title:
        division += f" ({fight.title})"
    lines = [
        event.name,
        "",
        f"Fight: {fight.matchup}",
        f"Division: {division}",
        f"Card: {fight.card.label if fight.card else 'TBD'}",
        f"Type: {', '.join(types)}",
    ]
    if selection.koreans:
        lines.append(f"Korean Fighter: {', '.join(f.name for f in selection.koreans)}")
    if event.venue:
        lines.append(f"Venue: {event.venue}")
    lines += ["", _time_note(selection, slot), "", "Official:", event.url]
    return "\n".join(lines)


def draft_for(selection: Selection) -> Draft | None:
    slot = slot_for(selection.event, selection.fight)
    if slot is None:
        log.warning(
            "%s: no date for bout %s, skipping", selection.event.name, selection.fight.fight_id
        )
        return None
    return Draft(
        event_id=selection.event.event_id,
        fight_id=selection.fight.fight_id,
        fighter_ids=tuple(
            f.fighter_id for f in selection.fight.fighters if f.fighter_id is not None
        ),
        summary=summary_for(selection),
        description=description_for(selection, slot),
        url=selection.event.url,
        start=slot.start.isoformat() if slot.start else None,
        end=slot.end.isoformat() if slot.end else None,
        day=slot.day.isoformat() if slot.day else None,
    )


def _vevent(entry: Entry) -> VEvent:
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
    event.add("summary", entry.summary)
    event.add("description", entry.description)
    event.add("url", entry.url)
    event.add("status", entry.status)
    event.add("transp", "TRANSPARENT")
    return event


def build_calendar(entries: Iterable[Entry]) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", CALENDAR_NAME)
    calendar.add("x-wr-caldesc", CALENDAR_DESCRIPTION)
    calendar.add("x-wr-timezone", "Asia/Seoul")
    calendar.add("refresh-interval", REFRESH_INTERVAL, parameters={"VALUE": "DURATION"})
    calendar.add("x-published-ttl", "PT6H")
    for entry in sorted(entries, key=lambda e: (e.begins, e.uid)):
        calendar.add_component(_vevent(entry))
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
