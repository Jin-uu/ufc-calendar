from datetime import date

import pytest
import requests
from helpers import (
    FakeResponse,
    FakeSession,
    fixture_session,
    fm_url,
    load_fixture,
    raw_event,
    raw_fight,
    raw_fighter,
    utc,
)

from ufc_calendar.models import Card, EventKind
from ufc_calendar.scraper import (
    ESPN_SCOREBOARD_URL,
    FetchError,
    apply_espn,
    fetch_espn_bouts,
    fetch_event_detail,
    name_keys,
    parse_event,
    scan_events,
    snapshot_event,
)


def fixture_event(event_id):
    return parse_event(load_fixture(f"fightmetric_{event_id}.json")["LiveEventDetail"])


# --- parsing -------------------------------------------------------------------------------


def test_parse_event_from_fightmetric_response():
    event = fixture_event(1332)

    assert event.name == "UFC Fight Night: Buckley vs. Malott"
    assert event.kind is EventKind.UFC
    assert event.url == "https://www.ufc.com/event/ufc-fight-night-october-17-2026"
    assert event.start == utc(2026, 10, 17, 21)
    assert event.start_reliable
    assert event.local_date == date(2026, 10, 17)
    assert event.venue == "Rogers Place, Edmonton, Canada"
    assert len(event.fights) == 13

    main = event.fights[0]
    assert (main.fight_id, main.order, main.card) == (13002, 1, Card.MAIN)
    assert main.card_start == utc(2026, 10, 18, 0)
    assert [f.name for f in main.fighters] == ["Joaquin Buckley", "Mike Malott"]  # red first
    assert main.weight_class == "Welterweight"

    korean = next(f for f in event.fights if f.fight_id == 13132)
    assert korean.card is Card.PRELIMS
    assert korean.fighters[1].name == "SuYoung You"
    assert korean.fighters[1].born_country_code == "KOR"


def test_parse_numbered_event_with_early_prelims_and_title():
    event = fixture_event(1335)

    assert event.url == "https://www.ufc.com/event/ufc-331"
    starts = {f.card: f.card_start for f in event.fights}
    assert starts == {
        Card.MAIN: utc(2026, 9, 20, 1),
        Card.PRELIMS: utc(2026, 9, 19, 23),
        Card.EARLY_PRELIMS: utc(2026, 9, 19, 21, 30),
    }
    assert event.fights[0].title is not None and "Title" in event.fights[0].title


def test_unconfirmed_card_segments_are_left_empty():
    event = fixture_event(1339)

    lee = next(f for f in event.fights if any(x.name == "JeongYeong Lee" for x in f.fighters))
    assert lee.card is None
    assert lee.card_start is None
    assert lee.order == 10


def test_event_without_time_zone_is_not_a_reliable_start():
    event = fixture_event(1348)

    assert not event.start_reliable
    assert event.local_date == date(2026, 11, 21)
    assert all(f.card is None for f in event.fights)


def test_partial_fields_are_tolerated():
    raw = raw_event(
        fights=[
            raw_fight(1, 1, "Main", None, raw_fighter(10, "Alpha", "One"),
                      raw_fighter(11, "Bravo", "Two", born=None), weight=None),
            {"FightOrder": 2, "Fighters": []},  # no FightId
            {"FightId": 3, "FightOrder": 3, "CardSegment": "Prelims1",
             "WeightClass": {"Description": "Catch", "CatchWeight": 150},
             "Fighters": [raw_fighter(12, "Charlie", "Three")]},
            {"FightId": 4, "CardSegment": "Mystery", "Fighters": None},
        ]
    )  # fmt: skip

    event = parse_event(raw)

    assert [f.fight_id for f in event.fights] == [1, 3, 4]
    first, catchweight, unknown = event.fights
    assert first.weight_class is None
    assert first.fighters[1].born_country_code is None
    assert catchweight.weight_class == "Catchweight (150 lb)"
    assert catchweight.matchup == "Charlie Three vs TBD"
    assert unknown.card is None and unknown.order is None and unknown.matchup == "TBD vs TBD"


def test_parse_event_requires_id_and_name():
    with pytest.raises(ValueError):
        parse_event({"Name": "UFC 999"})
    with pytest.raises(ValueError):
        parse_event({"EventId": 5, "Name": " "})


def test_event_kinds_and_urls():
    rtu = parse_event(raw_event(name="Road To UFC 5.3", org="Road to UFC"))
    dwcs = parse_event(raw_event(name="DWCS 10.6", org="Dana White's Contender Series"))
    other = parse_event(raw_event(name="Something Else", org="Other League"))

    assert rtu.kind is EventKind.ROAD_TO_UFC
    assert dwcs.kind is EventKind.CONTENDER_SERIES
    assert other.kind is EventKind.OTHER
    assert rtu.url == "https://www.ufc.com/events"


def test_snapshot_keeps_what_parsing_needs():
    raw = load_fixture("fightmetric_1332.json")["LiveEventDetail"]

    assert parse_event(snapshot_event(raw)) == parse_event(raw)


# --- HTTP ----------------------------------------------------------------------------------


def test_empty_event_means_unused_id():
    assert fetch_event_detail(FakeSession(), 999) is None


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(503, {"error": "unavailable"}),
        FakeResponse(200, body=b"<html>not json</html>"),
        FakeResponse(200, ["unexpected"]),
        requests.ConnectionError("network down"),
        requests.Timeout("too slow"),
    ],
)
def test_fetch_failures_raise_fetch_error(response):
    session = FakeSession({fm_url(5): response})

    with pytest.raises(FetchError):
        fetch_event_detail(session, 5)


def test_scan_stops_after_consecutive_unused_ids():
    event = load_fixture("fightmetric_1332.json")
    session = FakeSession({fm_url(100): event, fm_url(101): event, fm_url(103): event})

    result = scan_events(session, 100, max_misses=3, delay=0)

    assert sorted(result.raw_events) == [100, 101, 103]
    assert result.highest_id == 103
    assert session.calls[-1] == fm_url(106)


def test_scan_continues_through_gaps_below_the_known_maximum():
    event = load_fixture("fightmetric_1332.json")
    session = FakeSession({fm_url(100): event, fm_url(110): event})

    result = scan_events(session, 100, known_max_id=110, max_misses=3, delay=0)

    assert sorted(result.raw_events) == [100, 110]


def test_scan_falls_back_to_previous_snapshot_for_known_event():
    snapshot = load_fixture("fightmetric_1332.json")["LiveEventDetail"]
    session = FakeSession({fm_url(101): FakeResponse(503, {})})

    result = scan_events(session, 100, previous_raw={101: snapshot}, max_misses=2, delay=0)

    assert result.raw_events == {101: snapshot}
    assert result.stale_ids == [101]


def test_scan_fails_when_an_unknown_id_cannot_be_fetched():
    session = FakeSession({fm_url(101): requests.ConnectionError("down")})

    with pytest.raises(FetchError):
        scan_events(session, 100, max_misses=5, delay=0)


# --- ESPN ----------------------------------------------------------------------------------


def test_espn_supplies_nationality_and_missing_card_times():
    events = [fixture_event(1332), fixture_event(1339)]
    bouts = fetch_espn_bouts(fixture_session(), date(2026, 10, 13), date(2027, 1, 12))

    cache = apply_espn(events, bouts, {})

    fighters = {x.name: x for e in events for f in e.fights for x in f.fighters}
    assert (fighters["SuYoung You"].country, fighters["SuYoung You"].country_source) == (
        "KR",
        "espn",
    )
    # FightMetric only knows the birthplace (USA); ESPN has the nationality.
    assert fighters["Mike Malott"].born_country_code == "USA"
    assert fighters["Mike Malott"].country == "CA"
    # Case and accent differences between the sources still match.
    assert fighters["Julien Leblanc"].country_source == "espn"
    assert fighters["Cristian Perez"].country_source == "espn"

    lee_fight = next(f for f in events[1].fights if f.fighters[1].name == "JeongYeong Lee")
    assert lee_fight.espn_start == utc(2026, 10, 31, 21)
    assert cache[str(fighters["SuYoung You"].fighter_id)]["country"] == "KR"


def test_nationality_falls_back_to_cache_then_birthplace():
    raw = raw_event(fights=[raw_fight(1, 1, "Main", None,
                                      raw_fighter(10, "Alpha", "One", born="KOR"),
                                      raw_fighter(11, "Bravo", "Two", born=None))])  # fmt: skip
    cached = parse_event(raw)
    fallback = parse_event(raw)

    apply_espn([cached], [], {"10": {"country": "Canada"}, "11": {"country": "South Korea"}})
    apply_espn([fallback], [], {})

    one, two = cached.fights[0].fighters
    assert (one.country, one.country_source) == ("CA", "espn-cache")
    assert (two.country, two.country_source) == ("KR", "espn-cache")  # old name-style cache
    one, two = fallback.fights[0].fighters
    assert (one.country, one.country_source) == ("KR", "fightmetric-born")
    assert two.country is None


def test_espn_invalid_payload_raises():
    session = FakeSession({ESPN_SCOREBOARD_URL: {"unexpected": True}})

    with pytest.raises(FetchError):
        fetch_espn_bouts(session, date(2026, 10, 1), date(2026, 12, 31))


def test_name_keys_ignore_case_accents_spacing_and_order():
    assert name_keys("Cristian Pérez") & name_keys("Cristian Perez")
    assert name_keys("Julien LeBlanc") & name_keys("Julien Leblanc")
    assert name_keys("JeongYeong Lee") & name_keys("Jeong Yeong Lee")
    assert name_keys("Kwon WonIl") & name_keys("Wonil Kwon")
    assert not name_keys("Kyle Nelson") & name_keys("Kyle Daukaus")
