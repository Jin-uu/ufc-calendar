import logging

from helpers import event, fight, fighter, load_fixture, utc

from ufc_calendar.filter import DEFAULT_FEED, Feed, all_feeds, collect_bouts, matches
from ufc_calendar.models import Card, EventKind
from ufc_calendar.scraper import apply_espn, parse_event


def fixture_event(event_id):
    parsed = parse_event(load_fixture(f"fightmetric_{event_id}.json")["LiveEventDetail"])
    apply_espn([parsed], [], {})  # nationality from the FightMetric birthplace
    return parsed


def selected(feed, *events):
    return [b.fight.fight_id for b in collect_bouts(events) if matches(feed, b)]


def headliners(*events):
    return [
        (b.fight.fight_id, b.main, b.co_main) for b in collect_bouts(events) if b.main or b.co_main
    ]


def test_default_feed_selects_korean_fighter_main_and_co_main():
    assert selected(DEFAULT_FEED, fixture_event(1332)) == [13002, 13046, 13132]


def test_undercard_bout_without_the_country_is_excluded():
    card = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
        fight(2, 2, Card.MAIN, fighter("C"), fighter("D")),
        fight(3, 3, Card.MAIN, fighter("E"), fighter("F")),
        fight(4, 4, Card.PRELIMS, fighter("G"), fighter("H", country="JP")),
    )
    assert selected(DEFAULT_FEED, card) == [1, 2]
    assert selected(Feed("JP", headline="none"), card) == [4]
    assert selected(Feed("JP", headline="main"), card) == [1, 4]


def test_headline_options():
    card = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
        fight(2, 2, Card.MAIN, fighter("C"), fighter("D")),
        fight(3, 3, Card.MAIN, fighter("E"), fighter("F")),
    )
    assert selected(Feed(None, headline="both"), card) == [1, 2]
    assert selected(Feed(None, headline="main"), card) == [1]
    assert selected(Feed(None, headline="comain"), card) == [2]
    assert selected(Feed(None, headline="none"), card) == []


def test_gender_filter():
    women = fight(2, 2, Card.MAIN, fighter("C"), fighter("D"), weight_class="Women's Flyweight")
    catchweight = fight(3, 5, Card.PRELIMS, fighter("E", country="KR"), fighter("F"),
                        weight_class="Catchweight (130 lb)")  # fmt: skip
    catchweight.fighters[0].divisions = ("Women's Strawweight",)
    card = event(fight(1, 1, Card.MAIN, fighter("A"), fighter("B")), women, catchweight)

    assert selected(Feed("KR", gender="all"), card) == [1, 2, 3]
    assert selected(Feed("KR", gender="men"), card) == [1]
    assert selected(Feed("KR", gender="women"), card) == [2, 3]


def test_korean_main_event_is_a_single_bout():
    card = event(
        fight(1, 1, Card.MAIN, fighter("Korean Zombie", country="KR"), fighter("B")),
        fight(2, 2, Card.MAIN, fighter("C"), fighter("D")),
    )
    assert selected(DEFAULT_FEED, card) == [1, 2]


def test_missing_nationality_is_not_guessed_and_is_logged(caplog):
    card = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
        fight(9, 9, Card.PRELIMS, fighter("Kim Unknown", fighter_id=7, country=None), fighter("Y")),
    )

    with caplog.at_level(logging.WARNING):
        assert selected(DEFAULT_FEED, card) == [1]
    assert "nationality unknown for Kim Unknown" in caplog.text


def test_co_main_must_be_on_the_main_card():
    # Bout 2 of this card sits on the prelims while the main card is not filled in yet.
    assert headliners(fixture_event(1339)) == [(13133, True, False)]


def test_unassigned_co_main_needs_espn_evidence():
    main_time, prelim_time = utc(2026, 11, 8, 1), utc(2026, 11, 7, 22)
    without = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
        fight(2, 2, None, fighter("C"), fighter("D")),
    )
    with_espn = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B"), espn_start=main_time),
        fight(2, 2, None, fighter("C"), fighter("D"), espn_start=main_time),
        fight(3, 3, None, fighter("E"), fighter("F"), espn_start=prelim_time),
    )
    single_espn_time = event(
        fight(1, 1, None, fighter("A"), fighter("B"), espn_start=main_time),
        fight(2, 2, None, fighter("C"), fighter("D"), espn_start=main_time),
    )

    assert headliners(without) == [(1, True, False)]
    assert headliners(with_espn) == [(1, True, False), (2, False, True)]
    assert headliners(single_espn_time) == [(1, True, False)]


def test_unclear_order_does_not_create_main_or_co_main(caplog):
    card = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
        fight(2, 1, Card.MAIN, fighter("C"), fighter("D")),
        fight(3, None, Card.MAIN, fighter("E"), fighter("F")),
    )

    assert headliners(card) == []
    assert "not unique" in caplog.text


def test_side_events_only_contribute_country_bouts_when_enabled():
    for kind in (EventKind.ROAD_TO_UFC, EventKind.CONTENDER_SERIES):
        card = event(
            fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
            fight(3, 3, Card.MAIN, fighter("YoungJae Song", country="KR"), fighter("E")),
            kind=kind,
        )
        assert selected(Feed("KR", side=True), card) == [3]
        assert selected(Feed("KR", side=False), card) == []
        assert headliners(card) == []


def test_other_organisations_are_ignored():
    card = event(
        fight(1, 1, Card.MAIN, fighter("A", country="KR"), fighter("B")), kind=EventKind.OTHER
    )
    assert collect_bouts([card]) == []


def test_feed_slugs_and_combinations():
    assert DEFAULT_FEED.slug == "kr-all-both-rtu"
    assert Feed(None, "women", "main", True).slug == "all-women-main-ufc"
    assert Feed("JP", "men", "none", False).slug == "jp-men-none-ufc"

    feeds = all_feeds(["KR", "JP", "KR"])
    slugs = [f.slug for f in feeds]
    assert len(slugs) == len(set(slugs)) == 9 + 2 * 24
    assert DEFAULT_FEED in feeds
    assert Feed(None, headline="none", side=False) not in feeds  # selects nothing
