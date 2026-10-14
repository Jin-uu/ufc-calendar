"""Calendar state across runs: stable UIDs, change tracking and cancellations.

The state holds one master entry per bout; every feed is a filtered view of it (filter.py).
It is published next to the calendars as state.json and read back by the next run, so the
workflow never needs write access to the repository.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from .schedule import KST

log = logging.getLogger(__name__)

STATE_VERSION = 2
CONFIRMED = "CONFIRMED"
CANCELLED = "CANCELLED"
CANCELLED_NOTE = "이 경기는 취소되었거나 대진에서 빠졌습니다.\n\n"
HISTORY = timedelta(days=365)  # how long finished bouts stay on the calendar


class StateError(Exception):
    """The previous state file cannot be used."""


@dataclass(frozen=True)
class Draft:
    """A bout as computed from this run's data, before UID assignment."""

    event_id: int
    fight_id: int
    fighter_ids: tuple[int, ...]
    matchup: str
    kind: str  # EventKind value
    main: bool
    co_main: bool
    women: bool
    countries: tuple[str, ...]  # ISO alpha-2 of the fighters
    description: str
    url: str
    start: str | None = None  # ISO datetime in KST
    end: str | None = None
    day: str | None = None  # ISO date, for all-day entries


@dataclass
class Entry:
    uid: str
    event_id: int
    fight_ids: list[int]  # every FightMetric bout this entry has represented
    fighter_ids: list[int]
    matchup: str
    kind: str
    main: bool
    co_main: bool
    women: bool
    countries: list[str]
    description: str
    url: str
    start: str | None = None
    end: str | None = None
    day: str | None = None
    status: str = CONFIRMED
    sequence: int = 0
    last_modified: str = ""  # ISO datetime in UTC
    replaced_by: str | None = None  # set on the cancelled copy kept after a fighter change

    @property
    def begins(self) -> datetime:
        if self.start:
            return datetime.fromisoformat(self.start)
        if self.day:
            return datetime.combine(date.fromisoformat(self.day), time(), KST)
        raise ValueError(f"entry {self.uid} has neither start nor day")

    def content(self) -> tuple:
        return (
            self.matchup, self.kind, self.main, self.co_main, self.women, tuple(self.countries),
            self.description, self.url, self.start, self.end, self.day, self.status,
        )  # fmt: skip

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Entry:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


def reconcile(
    previous: Iterable[Entry],
    drafts: Iterable[Draft],
    cards: Mapping[int, Mapping[int, set[int]]],
    now: datetime,
    cutoff: datetime,
) -> tuple[list[Entry], Counter]:
    """Merge this run's drafts into the previous master entries.

    ``cards`` maps every event in this run's data to {fight id: fighter ids}. Entries starting
    before ``cutoff`` are history and kept as they are. A previous entry without a draft is
    dropped when its bout is still on a card (it is outside the window now) and marked
    cancelled when the bout is gone. When a fighter leaves a bout, a cancelled copy of the old
    version is kept (``replaced_by``) so feeds that only matched because of that fighter show
    the cancellation.
    """
    old = {e.uid: e for e in previous}
    stamp = now.astimezone(UTC).isoformat(timespec="seconds")
    live = {uid: e for uid, e in old.items() if not e.replaced_by}
    by_fight = {fight_id: uid for uid, e in live.items() for fight_id in e.fight_ids}
    result: dict[str, Entry] = {}
    changes: Counter = Counter()

    def vanished(entry: Entry) -> bool:
        card = cards.get(entry.event_id)
        return card is not None and not any(f in card for f in entry.fight_ids)

    def merge(uid: str, draft: Draft) -> None:
        before = old.get(uid)
        result[uid] = _merge(before, uid, draft, stamp, changes)
        if before and before.status == CONFIRMED:
            left = sorted(set(before.fighter_ids) - set(draft.fighter_ids))
            if left:
                ghost_uid = uid.replace("@", "-x" + "-".join(map(str, left)) + "@", 1)
                result[ghost_uid] = _cancelled(before, stamp, uid=ghost_uid, replaced_by=uid)
                changes["replaced"] += 1

    unmatched = []
    for draft in drafts:
        uid = by_fight.get(draft.fight_id)
        if uid and uid not in result:
            merge(uid, draft)
        else:
            unmatched.append(draft)
    for draft in unmatched:
        # A bout re-created after a fighter was replaced inherits the entry of the bout it
        # replaced: same event, old bout gone from the card, at least one fighter in common.
        uid = next(
            (
                uid
                for uid, e in live.items()
                if uid not in result
                and e.event_id == draft.event_id
                and vanished(e)
                and set(e.fighter_ids) & set(draft.fighter_ids)
            ),
            None,
        )
        if uid:
            merge(uid, draft)
        else:
            uid = f"fight-{draft.fight_id}@ufc-calendar"
            result[uid] = _merge(None, uid, draft, stamp, changes)

    for uid, entry in old.items():
        if uid in result:
            continue
        if entry.begins < cutoff:
            if entry.status == CONFIRMED and entry.begins >= now - HISTORY:
                result[uid] = entry
            else:
                changes["expired"] += 1
            continue
        if entry.replaced_by or entry.status == CANCELLED:
            result[uid] = entry
            continue
        card = cards.get(entry.event_id)
        if card is not None and any(f in card for f in entry.fight_ids):
            changes["removed"] += 1  # still scheduled, but outside the collection window
        else:
            result[uid] = _cancelled(entry, stamp)
            changes["cancelled"] += 1
    return sorted(result.values(), key=lambda e: (e.begins, e.uid)), changes


def _cancelled(entry: Entry, stamp: str, **overrides: Any) -> Entry:
    return replace(
        entry,
        status=CANCELLED,
        description=CANCELLED_NOTE + entry.description,
        sequence=entry.sequence + 1,
        last_modified=stamp,
        **overrides,
    )


def _merge(previous: Entry | None, uid: str, draft: Draft, stamp: str, changes: Counter) -> Entry:
    fight_ids = list(previous.fight_ids) if previous else []
    if draft.fight_id not in fight_ids:
        fight_ids.append(draft.fight_id)
    entry = Entry(
        uid=uid,
        event_id=draft.event_id,
        fight_ids=fight_ids,
        fighter_ids=list(draft.fighter_ids),
        matchup=draft.matchup,
        kind=draft.kind,
        main=draft.main,
        co_main=draft.co_main,
        women=draft.women,
        countries=list(draft.countries),
        description=draft.description,
        url=draft.url,
        start=draft.start,
        end=draft.end,
        day=draft.day,
        sequence=previous.sequence if previous else 0,
        last_modified=previous.last_modified if previous else stamp,
    )
    if previous is None:
        changes["added"] += 1
    elif entry.content() != previous.content():
        entry.sequence += 1
        entry.last_modified = stamp
        changes["updated"] += 1
    else:
        changes["unchanged"] += 1
    return entry


@dataclass
class State:
    entries: list[Entry] = field(default_factory=list)
    events: dict[int, dict] = field(default_factory=dict)  # snapshots of upcoming events
    upcoming: dict[int, str] = field(default_factory=dict)  # event id -> start (ISO, UTC)
    nationalities: dict[str, dict[str, str]] = field(default_factory=dict)  # ESPN, by fighter id
    countries: list[str] = field(default_factory=list)  # every country that has had a feed
    scan_from: int | None = None
    max_event_id: int | None = None
    generated_at: str | None = None

    def to_json(self) -> str:
        data = {"version": STATE_VERSION, **asdict(self)}
        return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> State:
        try:
            data = json.loads(text)
            version = data.get("version")
            if version not in (1, STATE_VERSION):
                raise StateError(f"unsupported state version {version!r}")
            entries = []
            if version == STATE_VERSION:
                entries = [Entry.from_dict(e) for e in data.get("entries", [])]
                for entry in entries:
                    entry.begins  # noqa: B018 - validates start/day
            else:
                log.info("Upgrading version 1 state: entries are rebuilt (UIDs stay the same)")
            return cls(
                entries=entries,
                events={int(k): v for k, v in data.get("events", {}).items()},
                upcoming={int(k): v for k, v in data.get("upcoming", {}).items()},
                nationalities=dict(data.get("nationalities", {})),
                countries=list(data.get("countries", [])),
                scan_from=data.get("scan_from"),
                max_event_id=data.get("max_event_id"),
                generated_at=data.get("generated_at"),
            )
        except StateError:
            raise
        except (ValueError, TypeError, AttributeError) as exc:
            raise StateError(f"invalid state file: {exc}") from exc
