from datetime import date, datetime

import pytest
from helpers import event, fight, fighter, utc
from icalendar import Calendar

from ufc_calendar.filter import DEFAULT_FEED, Feed, collect_bouts
from ufc_calendar.ics import (
    CalendarValidationError,
    build_calendar,
    calendar_name,
    draft_for,
    feed_entries,
    summary_for,
    validate_calendar,
)
from ufc_calendar.models import Card
from ufc_calendar.schedule import KST
from ufc_calendar.state import CANCELLED, Entry

HEADLINES = Feed(None, headline="both", side=False)


def entry(uid="fight-1@ufc-calendar", **overrides):
    values = dict(
        uid=uid,
        event_id=2000,
        fight_ids=[1],
        fighter_ids=[10, 11],
        matchup="A vs B",
        kind="ufc",
        main=True,
        co_main=False,
        women=False,
        countries=["US"],
        description="UFC Fight Night\n\nFight: A vs B",
        url="https://www.ufc.com/event/ufc-333",
        start="2026-10-25T03:00:00+09:00",
        end="2026-10-25T04:00:00+09:00",
        last_modified="2026-10-14T00:00:00+00:00",
    )
    values.update(overrides)
    return Entry(**values)


def parse(data: bytes):
    return Calendar.from_ical(data).walk("VEVENT")


def test_builds_a_valid_calendar():
    all_day = entry("fight-2@ufc-calendar", day="2026-11-21", start=None, end=None)

    data = build_calendar([entry(), all_day], HEADLINES)

    assert validate_calendar(data) == 2
    text = data.decode()
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")
    assert "VERSION:2.0" in text and "PRODID:" in text
    assert "X-WR-TIMEZONE:Asia/Seoul" in text
    assert "BEGIN:VTIMEZONE\r\nTZID:Asia/Seoul" in text
    assert "DTSTART;TZID=Asia/Seoul:20261025T030000" in text

    timed, all_day_event = parse(data)
    assert timed.decoded("DTSTART") == datetime(2026, 10, 25, 3, tzinfo=KST)
    assert str(timed["SUMMARY"]) == "🥊 UFC Main | A vs B"
    assert str(timed["STATUS"]) == "CONFIRMED"
    assert str(timed["URL"]) == "https://www.ufc.com/event/ufc-333"
    assert all_day_event.decoded("DTSTART") == date(2026, 11, 21)
    assert all_day_event.decoded("DTEND") == date(2026, 11, 22)


def test_summary_depends_on_the_feed():
    korean_main = entry(countries=["KR", "US"])
    korean_undercard = entry(main=False, countries=["KR"], kind="road_to_ufc")
    cancelled = entry(co_main=True, main=False, status=CANCELLED)

    assert summary_for(korean_main, DEFAULT_FEED) == "🇰🇷 UFC Main | A vs B"
    assert summary_for(korean_main, Feed("US")) == "🇺🇸 UFC Main | A vs B"
    assert summary_for(korean_main, HEADLINES) == "🥊 UFC Main | A vs B"
    assert summary_for(korean_undercard, DEFAULT_FEED) == "🇰🇷 Road to UFC | A vs B"
    assert summary_for(cancelled, HEADLINES) == "❌ 취소 | 🥊 UFC Co-Main | A vs B"


def test_cancelled_copy_only_shows_where_the_current_bout_does_not():
    current = entry(countries=["US"], fighter_ids=[10, 12])  # Korean fighter 11 replaced
    ghost = entry("fight-1-x11@ufc-calendar", countries=["KR", "US"], status=CANCELLED,
                  replaced_by=current.uid)  # fmt: skip

    assert [e.uid for e in feed_entries([current, ghost], Feed("KR", headline="none"))] == [
        ghost.uid
    ]
    assert [e.uid for e in feed_entries([current, ghost], DEFAULT_FEED)] == [current.uid]


def test_special_characters_and_newlines_round_trip():
    matchup = "Lone'er Kavanagh vs O'Malley, Jr.; \"Sugar\" \\ test"
    description = "Line 1\nLine 2, with comma; semicolon\n\n※ 줄바꿈\\백슬래시"

    data = build_calendar([entry(matchup=matchup, description=description)], HEADLINES)

    validate_calendar(data)
    (parsed,) = parse(data)
    assert str(parsed["SUMMARY"]) == f"🥊 UFC Main | {matchup}"
    assert str(parsed["DESCRIPTION"]) == description


def test_korean_and_emoji_are_utf8_and_folded_safely():
    description = (
        "※ 표시된 시간은 메인 카드 시작 시간이며, 실제 경기 시작 시간은 다를 수 있습니다. " * 4
    )

    data = build_calendar(
        [entry(matchup="정찬성 vs 🥊 Fighter", description=description, countries=["KR"])],
        DEFAULT_FEED,
    )

    validate_calendar(data)  # includes the 75-octet line limit
    assert max(len(line) for line in data.split(b"\r\n")) <= 75
    (parsed,) = parse(data)
    assert str(parsed["SUMMARY"]) == "🇰🇷 UFC Main | 정찬성 vs 🥊 Fighter"
    assert str(parsed["DESCRIPTION"]) == description
    assert "X-WR-CALNAME:UFC 🇰🇷 대한민국 + 메인·코메인" in data.decode().replace("\r\n ", "")


def test_output_is_deterministic():
    later = entry("fight-2@ufc-calendar", start="2026-11-01T09:00:00+09:00",
                  end="2026-11-01T10:00:00+09:00")  # fmt: skip

    assert build_calendar([later, entry()], HEADLINES) == build_calendar(
        [entry(), later], HEADLINES
    )


def test_validation_rejects_broken_calendars():
    good = build_calendar([entry()], HEADLINES).decode()
    vevent = "BEGIN:VEVENT" + good.split("BEGIN:VEVENT")[1].split("END:VEVENT")[0]
    duplicate = good.replace("END:VCALENDAR", vevent + "END:VEVENT\r\nEND:VCALENDAR")

    with pytest.raises(CalendarValidationError, match="duplicate UID"):
        validate_calendar(duplicate.encode())
    with pytest.raises(CalendarValidationError):
        validate_calendar(b"not a calendar")
    with pytest.raises(CalendarValidationError, match="DTSTART"):
        validate_calendar(good.replace("DTSTART;", "X-DTSTART;").encode())


def test_empty_calendar_is_valid():
    assert validate_calendar(build_calendar([], HEADLINES)) == 0


def test_calendar_names():
    assert calendar_name(DEFAULT_FEED) == "UFC 🇰🇷 대한민국 + 메인·코메인"
    assert calendar_name(Feed(None, "women", "main")) == "UFC 메인 · 여자부"
    assert calendar_name(Feed("JP", "men", "none")) == "UFC 🇯🇵 일본 · 남자부"


def test_draft_contains_the_required_details():
    card = event(
        fight(1, 1, Card.MAIN, fighter("A", 10, country="KR"), fighter("B", 11),
              card_start=utc(2026, 11, 8, 1)),
        name="UFC 999: A vs. B",
    )  # fmt: skip
    card.fights[0].title = "UFC Lightweight Title"

    draft = draft_for(collect_bouts([card])[0])

    assert (draft.matchup, draft.main, draft.co_main, draft.women) == ("A vs B", True, False, False)
    assert draft.countries == ("KR", "US")
    assert draft.start == "2026-11-08T10:00:00+09:00"
    assert draft.end == "2026-11-08T11:00:00+09:00"
    assert draft.fighter_ids == (10, 11)
    lines = draft.description.split("\n")
    assert lines[0] == "UFC 999: A vs. B"
    assert "Fight: A vs B" in lines
    assert "Fighters: A (South Korea), B (USA)" in lines
    assert "Division: Lightweight (UFC Lightweight Title)" in lines
    assert "Card: Main Card" in lines
    assert "Type: Main Event" in lines
    assert "※ 표시된 시간은 메인 카드 시작 시간이며," in lines
    assert lines[-2:] == ["Official:", card.url]


def test_draft_explains_fallback_times():
    card = event(fight(1, 1, None, fighter("A"), fighter("B")), start_reliable=False)

    draft = draft_for(collect_bouts([card])[0])

    assert draft.start is None and draft.day == "2026-11-07"
    assert "Card: TBD" in draft.description
    assert "날짜(현지 기준)만 표시" in draft.description
