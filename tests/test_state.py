from datetime import timedelta

import pytest
from helpers import utc

from ufc_calendar.ics import build_calendar
from ufc_calendar.state import (
    CANCELLED,
    CONFIRMED,
    Draft,
    Entry,
    State,
    StateError,
    reconcile,
)

NOW = utc(2026, 10, 14)
CUTOFF = NOW - timedelta(days=1)


def draft(fight_id=1, fighters=(10, 11), summary="🥊 UFC Main | A vs B",
          start="2026-11-08T10:00:00+09:00", day=None, event_id=2000) -> Draft:  # fmt: skip
    end = None if start is None else start.replace("T10:", "T11:")
    return Draft(
        event_id=event_id,
        fight_id=fight_id,
        fighter_ids=tuple(fighters),
        summary=summary,
        description=f"Fight: {summary}",
        url="https://www.ufc.com/event/x",
        start=start,
        end=end,
        day=day,
    )


def run(previous, drafts, cards, now=NOW):
    return reconcile(previous, drafts, cards, now, now - timedelta(days=1))


CARD = {2000: {1: {10, 11}, 2: {12, 13}}}


def test_same_bout_keeps_its_uid_and_is_unchanged():
    first, _ = run([], [draft()], CARD)
    second, changes = run(first, [draft()], CARD, now=NOW + timedelta(days=1))

    assert [e.uid for e in first] == ["fight-1@ufc-calendar"]
    assert second == first
    assert changes == {"unchanged": 1}


def test_time_change_keeps_uid_and_bumps_sequence():
    first, _ = run([], [draft()], CARD)
    later = NOW + timedelta(days=1)

    second, changes = run(first, [draft(start="2026-11-08T09:00:00+09:00")], CARD, now=later)

    (entry,) = second
    assert entry.uid == first[0].uid
    assert entry.start == "2026-11-08T09:00:00+09:00"
    assert entry.sequence == 1
    assert entry.last_modified == later.isoformat(timespec="seconds")
    assert changes == {"updated": 1}


def test_event_date_change_keeps_uid():
    first, _ = run([], [draft()], CARD)

    second, _ = run(first, [draft(start="2026-11-15T10:00:00+09:00")], CARD)

    assert second[0].uid == first[0].uid
    assert second[0].start.startswith("2026-11-15")


def test_replaced_fighter_on_the_same_bout_keeps_uid():
    first, _ = run([], [draft()], CARD)
    card = {2000: {1: {10, 99}}}

    second, changes = run(first, [draft(fighters=(10, 99), summary="🥊 UFC Main | A vs Z")], card)

    assert second[0].uid == first[0].uid
    assert second[0].summary == "🥊 UFC Main | A vs Z"
    assert changes == {"updated": 1}


def test_bout_recreated_after_replacement_keeps_uid():
    first, _ = run([], [draft()], CARD)
    card = {2000: {5: {10, 99}, 2: {12, 13}}}  # bout 1 is gone, bout 5 has fighter 10

    second, changes = run(first, [draft(fight_id=5, fighters=(10, 99), summary="A vs Z")], card)

    (entry,) = second
    assert entry.uid == "fight-1@ufc-calendar"
    assert entry.fight_ids == [1, 5]
    assert entry.status == CONFIRMED
    assert changes == {"updated": 1}


def test_processing_the_same_data_twice_creates_no_duplicates():
    drafts = [draft(), draft(fight_id=2, fighters=(12, 13), summary="🥊 UFC Co-Main | C vs D")]
    first, _ = run([], drafts, CARD)
    second, _ = run(first, drafts, CARD)

    assert len({e.uid for e in second}) == len(second) == 2
    assert build_calendar(first) == build_calendar(second)


def test_removed_bout_is_marked_cancelled_and_stays_cancelled():
    first, _ = run([], [draft()], CARD)
    card = {2000: {2: {12, 13}}}

    second, changes = run(first, [], card)
    third, changes_again = run(second, [], card)

    (entry,) = second
    assert entry.status == CANCELLED
    assert entry.summary == "❌ 취소 | 🥊 UFC Main | A vs B"
    assert entry.description.startswith("이 경기는 취소되었거나")
    assert entry.sequence == 1
    assert changes == {"cancelled": 1}
    assert third == second and changes_again == {"unchanged": 1}


def test_cancelled_bout_that_returns_is_confirmed_again():
    first, _ = run([], [draft()], CARD)
    cancelled, _ = run(first, [], {2000: {}})

    restored, changes = run(cancelled, [draft()], CARD)

    assert restored[0].status == CONFIRMED
    assert restored[0].summary == "🥊 UFC Main | A vs B"
    assert restored[0].sequence == 2
    assert changes == {"updated": 1}


def test_event_that_disappeared_cancels_its_entries():
    first, _ = run([], [draft()], CARD)

    second, changes = run(first, [], {})

    assert second[0].status == CANCELLED
    assert changes == {"cancelled": 1}


def test_bout_no_longer_qualifying_is_removed_not_cancelled():
    # e.g. the bout is still on the card but is no longer the co-main event
    first, _ = run([], [draft()], CARD)

    second, changes = run(first, [], CARD)

    assert second == []
    assert changes == {"removed": 1}


def test_korean_fighter_replaced_on_same_bout_cancels_the_entry():
    first, _ = run([], [draft(summary="🇰🇷 UFC | K vs B")], CARD)
    card = {2000: {1: {99, 11}}}  # fighter 10 (the Korean fighter) was replaced

    second, changes = run(first, [], card)

    assert second[0].status == CANCELLED
    assert changes == {"cancelled": 1}


def test_finished_bouts_stay_as_history_and_old_ones_expire():
    first, _ = run([], [draft(), draft(fight_id=2, fighters=(12, 13), summary="C vs D")], CARD)
    cancelled, _ = run(first, [draft()], {2000: {1: {10, 11}}})  # bout 2 cancelled

    after_event = utc(2026, 11, 10)
    history, changes = run(cancelled, [], {}, now=after_event)
    much_later, later_changes = run(history, [], {}, now=after_event + timedelta(days=400))

    assert [(e.uid, e.status) for e in history] == [("fight-1@ufc-calendar", CONFIRMED)]
    assert changes == {"expired": 1}
    assert much_later == [] and later_changes == {"expired": 1}


def test_state_round_trip():
    all_day = draft(fight_id=2, fighters=(12, 13), start=None, day="2026-11-07")
    entries, _ = run([], [draft(), all_day], CARD)
    state = State(
        entries=entries,
        events={2000: {"EventId": 2000}},
        upcoming={2000: "2026-11-07T22:00:00+00:00"},
        nationalities={"10": {"name": "A", "country": "South Korea"}},
        scan_from=1332,
        max_event_id=1349,
        generated_at=NOW.isoformat(),
    )

    assert State.from_json(state.to_json()) == state


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"version": 99}',
        '{"version": 1, "entries": [{"uid": "x"}]}',
        '{"version": 1, "entries": [{"uid": "x", "event_id": 1, "fight_ids": [], '
        '"fighter_ids": [], "summary": "", "description": "", "url": ""}]}',
    ],
)
def test_invalid_state_is_rejected(text):
    with pytest.raises(StateError):
        State.from_json(text)


def test_entry_requires_a_start_or_day():
    with pytest.raises(ValueError):
        Entry("x", 1, [], [], "", "", "").begins  # noqa: B018
