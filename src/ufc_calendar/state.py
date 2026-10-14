"""Calendar state across runs: stable UIDs, change tracking and cancellations.

The state is published next to ufc.ics as state.json and read back by the next run, so the
workflow never needs write access to the repository.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from .schedule import KST

STATE_VERSION = 1
CONFIRMED = "CONFIRMED"
CANCELLED = "CANCELLED"
CANCELLED_PREFIX = "❌ 취소 | "
CANCELLED_NOTE = "이 경기는 취소되었거나 대진에서 빠졌습니다.\n\n"
HISTORY = timedelta(days=365)  # how long finished bouts stay on the calendar


class StateError(Exception):
    """The previous state file cannot be used."""


@dataclass(frozen=True)
class Draft:
    """A calendar entry as computed from this run's data, before UID assignment."""

    event_id: int
    fight_id: int
    fighter_ids: tuple[int, ...]
    summary: str
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
    summary: str
    description: str
    url: str
    start: str | None = None
    end: str | None = None
    day: str | None = None
    status: str = CONFIRMED
    sequence: int = 0
    last_modified: str = ""  # ISO datetime in UTC

    @property
    def begins(self) -> datetime:
        if self.start:
            return datetime.fromisoformat(self.start)
        if self.day:
            return datetime.combine(date.fromisoformat(self.day), time(), KST)
        raise ValueError(f"entry {self.uid} has neither start nor day")

    def content(self) -> tuple:
        return (
            self.summary,
            self.description,
            self.url,
            self.start,
            self.end,
            self.day,
            self.status,
        )

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
    """Merge this run's drafts into the previous entries.

    ``cards`` maps every event in this run's data to {fight id: fighter ids}. Entries starting
    before ``cutoff`` are history and kept as they are. A previous entry without a draft is
    dropped when its bout is still on the card (it no longer qualifies, e.g. it is not the
    co-main any more) and marked cancelled when the bout or its fighters are gone.
    """
    old = {e.uid: e for e in previous}
    stamp = now.astimezone(UTC).isoformat(timespec="seconds")
    by_fight = {fight_id: uid for uid, e in old.items() for fight_id in e.fight_ids}
    result: dict[str, Entry] = {}
    changes: Counter = Counter()

    def vanished(entry: Entry) -> bool:
        card = cards.get(entry.event_id)
        return card is not None and not any(f in card for f in entry.fight_ids)

    unmatched = []
    for draft in drafts:
        uid = by_fight.get(draft.fight_id)
        if uid and uid not in result:
            result[uid] = _merge(old[uid], uid, draft, stamp, changes)
        else:
            unmatched.append(draft)
    for draft in unmatched:
        # A bout re-created after a fighter was replaced inherits the entry of the bout it
        # replaced: same event, old bout gone from the card, at least one fighter in common.
        uid = next(
            (
                uid
                for uid, e in old.items()
                if uid not in result
                and e.event_id == draft.event_id
                and vanished(e)
                and set(e.fighter_ids) & set(draft.fighter_ids)
            ),
            None,
        )
        if uid:
            result[uid] = _merge(old[uid], uid, draft, stamp, changes)
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
        card = cards.get(entry.event_id)
        still_scheduled = card is not None and any(
            f in card and set(entry.fighter_ids) <= card[f] for f in entry.fight_ids
        )
        if still_scheduled:
            changes["removed"] += 1
        elif entry.status == CANCELLED:
            result[uid] = entry
            changes["unchanged"] += 1
        else:
            result[uid] = replace(
                entry,
                status=CANCELLED,
                summary=CANCELLED_PREFIX + entry.summary,
                description=CANCELLED_NOTE + entry.description,
                sequence=entry.sequence + 1,
                last_modified=stamp,
            )
            changes["cancelled"] += 1
    return sorted(result.values(), key=lambda e: (e.begins, e.uid)), changes


def _merge(previous: Entry | None, uid: str, draft: Draft, stamp: str, changes: Counter) -> Entry:
    fight_ids = list(previous.fight_ids) if previous else []
    if draft.fight_id not in fight_ids:
        fight_ids.append(draft.fight_id)
    entry = Entry(
        uid=uid,
        event_id=draft.event_id,
        fight_ids=fight_ids,
        fighter_ids=list(draft.fighter_ids),
        summary=draft.summary,
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
            if data.get("version") != STATE_VERSION:
                raise StateError(f"unsupported state version {data.get('version')!r}")
            entries = [Entry.from_dict(e) for e in data.get("entries", [])]
            for entry in entries:
                entry.begins  # noqa: B018 - validates start/day
            return cls(
                entries=entries,
                events={int(k): v for k, v in data.get("events", {}).items()},
                upcoming={int(k): v for k, v in data.get("upcoming", {}).items()},
                nationalities=dict(data.get("nationalities", {})),
                scan_from=data.get("scan_from"),
                max_event_id=data.get("max_event_id"),
                generated_at=data.get("generated_at"),
            )
        except StateError:
            raise
        except (ValueError, TypeError, AttributeError) as exc:
            raise StateError(f"invalid state file: {exc}") from exc
