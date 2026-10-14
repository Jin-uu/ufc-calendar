import logging

import pytest
from helpers import event, fight, fighter, load_fixture, utc

from ufc_calendar.filter import is_korean, select_fights
from ufc_calendar.ics import summary_for
from ufc_calendar.models import Card, EventKind
from ufc_calendar.scraper import apply_espn, parse_event


def selected_ids(*events):
    return [s.fight.fight_id for s in select_fights(events)]


def fixture_event(event_id):
    parsed = parse_event(load_fixture(f"fightmetric_{event_id}.json")["LiveEventDetail"])
    apply_espn([parsed], [], {})  # nationality from the FightMetric birthplace
    return parsed


def test_selects_korean_fighter_main_and_co_main_from_real_card():
    assert selected_ids(fixture_event(1332)) == [13002, 13046, 13132]


def test_regular_undercard_bout_is_excluded():
    card = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
        fight(2, 2, Card.MAIN, fighter("C"), fighter("D")),
        fight(3, 3, Card.MAIN, fighter("E"), fighter("F")),
        fight(4, 4, Card.PRELIMS, fighter("G"), fighter("H")),
    )
    assert selected_ids(card) == [1, 2]


def test_korean_fighter_on_prelims_is_included():
    card = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
        fight(5, 7, Card.PRELIMS, fighter("Seokhyeon Ko", country="KOR"), fighter("X")),
    )

    selection = select_fights([card])[-1]

    assert selection.fight.fight_id == 5
    assert [f.name for f in selection.koreans] == ["Seokhyeon Ko"]
    assert summary_for(selection) == "🇰🇷 UFC | Seokhyeon Ko vs X"


def test_korean_main_event_is_a_single_entry_with_both_labels():
    card = event(
        fight(1, 1, Card.MAIN, fighter("Korean Zombie", country="South Korea"), fighter("B")),
        fight(2, 2, Card.MAIN, fighter("C"), fighter("D")),
    )

    selections = select_fights([card])

    assert [s.fight.fight_id for s in selections] == [1, 2]
    assert summary_for(selections[0]) == "🇰🇷 UFC Main | Korean Zombie vs B"
    assert summary_for(selections[1]) == "🥊 UFC Co-Main | C vs D"


def test_korean_co_main_is_a_single_entry():
    card = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
        fight(2, 2, Card.MAIN, fighter("C"), fighter("Dooho Choi", country="KOR")),
    )

    selections = select_fights([card])

    assert len(selections) == 2
    assert summary_for(selections[0]) == "🥊 UFC Main | A vs B"
    assert summary_for(selections[1]) == "🇰🇷 UFC Co-Main | C vs Dooho Choi"


def test_missing_nationality_is_not_guessed_and_is_logged(caplog):
    card = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
        fight(9, 9, Card.PRELIMS, fighter("Kim Unknown", fighter_id=7, country=None), fighter("Y")),
    )

    with caplog.at_level(logging.WARNING):
        assert selected_ids(card) == [1]
    assert "nationality unknown for Kim Unknown" in caplog.text


@pytest.mark.parametrize(
    ("country", "expected"),
    [
        ("KOR", True),
        ("South Korea", True),
        ("Republic of Korea", True),
        (" kor ", True),
        ("North Korea", False),
        ("PRK", False),
        ("JPN", False),
        (None, None),
        ("", None),
    ],
)
def test_is_korean(country, expected):
    assert is_korean(fighter("X", country=country)) is expected


def test_co_main_must_be_on_the_main_card():
    # Bout 2 of this card sits on the prelims while the main card is not filled in yet.
    assert selected_ids(fixture_event(1339)) == [13133, 13178]


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

    assert selected_ids(without) == [1]
    assert selected_ids(with_espn) == [1, 2]
    assert selected_ids(single_espn_time) == [1]


def test_unclear_order_does_not_create_main_or_co_main(caplog):
    card = event(
        fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
        fight(2, 1, Card.MAIN, fighter("C"), fighter("D")),
        fight(3, None, Card.MAIN, fighter("E"), fighter("F")),
    )

    assert selected_ids(card) == []
    assert "not unique" in caplog.text


def test_road_to_ufc_and_contender_series_only_include_koreans():
    for kind in (EventKind.ROAD_TO_UFC, EventKind.CONTENDER_SERIES):
        card = event(
            fight(1, 1, Card.MAIN, fighter("A"), fighter("B")),
            fight(2, 2, Card.MAIN, fighter("C"), fighter("D")),
            fight(3, 3, Card.MAIN, fighter("YoungJae Song", country="KOR"), fighter("E")),
            kind=kind,
        )
        selections = select_fights([card])
        assert [s.fight.fight_id for s in selections] == [3]
        assert not selections[0].main


def test_road_to_ufc_title():
    card = event(
        fight(3, 1, Card.MAIN, fighter("BoHyun Park", country="KOR"), fighter("E")),
        kind=EventKind.ROAD_TO_UFC,
    )
    assert summary_for(select_fights([card])[0]) == "🇰🇷 Road to UFC | BoHyun Park vs E"


def test_other_organisations_are_ignored():
    card = event(
        fight(1, 1, Card.MAIN, fighter("A", country="KOR"), fighter("B")), kind=EventKind.OTHER
    )
    assert selected_ids(card) == []
