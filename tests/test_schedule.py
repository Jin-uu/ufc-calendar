from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from helpers import event, fight, fighter, load_fixture, utc

from ufc_calendar.models import Card
from ufc_calendar.schedule import KST, TimeSource, parse_timestamp, slot_for, to_kst
from ufc_calendar.scraper import parse_event

LAS_VEGAS = ZoneInfo("America/Los_Angeles")


def kst(*args):
    return datetime(*args, tzinfo=KST)


@pytest.mark.parametrize(
    "value",
    ["2026-10-18T00:00Z", "2026-10-18T00:00:00Z", "2026-10-18T00:00:00+00:00",
     "2026-10-17T17:00:00-07:00", "2026-10-18T09:00+09:00"],
)  # fmt: skip
def test_parse_timestamp_normalises_to_utc(value):
    assert parse_timestamp(value) == utc(2026, 10, 18, 0)


@pytest.mark.parametrize("value", [None, "", "TBD", "2026-10-18T00:00:00", 12345])
def test_parse_timestamp_rejects_missing_or_ambiguous_values(value):
    assert parse_timestamp(value) is None


def test_utc_to_seoul():
    converted = to_kst(utc(2026, 10, 18, 0))

    assert converted == kst(2026, 10, 18, 9)
    assert converted.utcoffset() == timedelta(hours=9)
    assert converted.tzinfo.key == "Asia/Seoul"


def test_us_daylight_saving_time_change():
    # 17:00 in Las Vegas is 09:00 KST under PDT (Oct 31) but 10:00 KST under PST (Nov 7).
    before = datetime(2026, 10, 31, 17, tzinfo=LAS_VEGAS)
    after = datetime(2026, 11, 7, 17, tzinfo=LAS_VEGAS)

    assert to_kst(before) == kst(2026, 11, 1, 9)
    assert to_kst(after) == kst(2026, 11, 8, 10)
    # The same instants as the feeds report them (UTC "Z" timestamps).
    assert to_kst(parse_timestamp("2026-11-01T00:00Z")) == kst(2026, 11, 1, 9)
    assert to_kst(parse_timestamp("2026-11-08T01:00Z")) == kst(2026, 11, 8, 10)


def test_each_card_uses_its_own_start_time():
    ufc_331 = parse_event(load_fixture("fightmetric_1335.json")["LiveEventDetail"])
    by_card = {f.card: slot_for(ufc_331, f) for f in ufc_331.fights}

    assert by_card[Card.MAIN].start == kst(2026, 9, 20, 10)
    assert by_card[Card.PRELIMS].start == kst(2026, 9, 20, 8)
    assert by_card[Card.EARLY_PRELIMS].start == kst(2026, 9, 20, 6, 30)
    assert all(s.source is TimeSource.CARD for s in by_card.values())


def test_bouts_on_the_same_card_share_the_start_time():
    card = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B"), card_start=utc(2026, 11, 8, 1)),
        fight(2, 2, Card.MAIN, fighter("C"), fighter("D")),
    )

    assert slot_for(card, card.fights[1]) == slot_for(card, card.fights[0])


def test_missing_card_time_falls_back_to_espn_card_time():
    card = event(fight(1, 1, Card.MAIN, fighter("A"), fighter("B"), espn_start=utc(2026, 11, 8, 1)))

    slot = slot_for(card, card.fights[0])

    assert (slot.source, slot.start) == (TimeSource.ESPN_CARD, kst(2026, 11, 8, 10))


def test_missing_card_time_falls_back_to_event_start():
    card = event(fight(1, 9, None, fighter("A"), fighter("B")), start=utc(2026, 11, 7, 22))

    slot = slot_for(card, card.fights[0])

    assert (slot.source, slot.start) == (TimeSource.EVENT, kst(2026, 11, 8, 7))
    assert slot.end == kst(2026, 11, 8, 8)


def test_unreliable_event_start_is_not_used():
    card = event(fight(1, 1, None, fighter("A"), fighter("B")), start_reliable=False)

    slot = slot_for(card, card.fights[0])

    assert (slot.source, slot.start, slot.day) == (TimeSource.DATE_ONLY, None, date(2026, 11, 7))
    assert slot.end is None


def test_no_date_at_all_gives_no_slot():
    card = event(fight(1, 1, None, fighter("A"), fighter("B")))
    card.start = card.local_date = None

    assert slot_for(card, card.fights[0]) is None
