"""Command line entry point: collect, filter, build, validate and write the calendars.

Nothing is written unless every step succeeds, so a failed run leaves the previously published
calendars untouched.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from .countries import country_name, flag
from .filter import DEFAULT_COUNTRY, DEFAULT_FEED, all_feeds, collect_bouts, matches
from .ics import (
    CalendarValidationError,
    build_calendar,
    draft_for,
    summary_for,
    validate_calendar,
)
from .models import Event, EventKind
from .schedule import KST, parse_timestamp
from .scraper import (
    TIMEOUT,
    FetchError,
    apply_espn,
    fetch_espn_bouts,
    make_session,
    parse_event,
    scan_events,
    snapshot_event,
)
from .state import CONFIRMED, Entry, State, StateError, reconcile

log = logging.getLogger("ufc_calendar")

DEFAULT_SEED_EVENT_ID = 1330  # a FightMetric event id from shortly before the first deployment
LOOKBACK = timedelta(days=1)  # events that started within this period are still refreshed


class AbortError(Exception):
    """The collected data looks wrong; nothing is published."""


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ufc-calendar",
        description="Build iCalendar feeds of UFC bouts filtered by country, gender and headline.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument(
        "--previous-state",
        help="state.json of the previous run, as a path or URL (default: <output-dir>/state.json)",
    )
    parser.add_argument("--days", type=int, default=90, help="how far ahead to collect events")
    parser.add_argument(
        "--seed-event-id",
        type=int,
        default=DEFAULT_SEED_EVENT_ID,
        help="first FightMetric event id to scan when there is no previous state",
    )
    parser.add_argument(
        "--allow-drop",
        action="store_true",
        help="publish even if many events or entries disappeared since the previous run",
    )
    parser.add_argument("--delay", type=float, default=0.25, help=argparse.SUPPRESS)
    parser.add_argument("--now", type=datetime.fromisoformat, help=argparse.SUPPRESS)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        run(args, make_session())
    except (FetchError, StateError, AbortError, CalendarValidationError) as exc:
        log.error("Calendar not updated: %s", exc)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error title=Calendar not updated::{exc}")
        return 1
    return 0


def run(args: argparse.Namespace, session: requests.Session) -> list[Entry]:
    now = args.now or datetime.now(UTC)
    now = (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(UTC)
    cutoff, horizon = now - LOOKBACK, now + timedelta(days=args.days)
    source = args.previous_state or args.output_dir / "state.json"
    previous = read_previous_state(source, session) or State()

    # 1. Collect and validate the UFC data.
    scan = scan_events(
        session,
        previous.scan_from or args.seed_event_id,
        known_max_id=previous.max_event_id,
        previous_raw=previous.events,
        delay=args.delay,
    )
    events = parse_events(scan.raw_events)
    upcoming = [
        e for e in events if e.kind is not EventKind.OTHER and e.start and e.start >= cutoff
    ]
    window = [e for e in upcoming if e.start <= horizon]
    log.info("%d upcoming events, %d within %d days", len(upcoming), len(window), args.days)
    if not window:
        log.warning("No UFC events are scheduled in the next %d days", args.days)
    check_missing_events(previous, {e.event_id for e in upcoming}, cutoff, args.allow_drop)

    # 2. Nationalities and missing card times from ESPN (optional).
    try:
        bouts = fetch_espn_bouts(session, cutoff.date(), horizon.date())
    except FetchError as exc:
        log.warning("ESPN unavailable (%s); using cached and FightMetric nationalities", exc)
        bouts = []
    nationalities = apply_espn(window, bouts, previous.nationalities)

    # 3. Merge every bout into the master entries.
    bouts = collect_bouts(window)
    drafts = [d for d in map(draft_for, bouts) if d]
    entries, changes = reconcile(previous.entries, drafts, cards_of(events), now, cutoff)
    log.info("Calendar changes: %s", dict(sorted(changes.items())))
    check_cancellations(previous.entries, changes, cutoff, args.allow_drop)

    # 4. Build and validate one calendar per feed, then write everything.
    countries = sorted(
        set(previous.countries) | {c for b in bouts for c in b.countries} | {DEFAULT_COUNTRY}
    )
    files: dict[str, bytes] = {}
    for feed in all_feeds(countries):
        calendar = build_calendar(entries, feed)
        validate_calendar(calendar)
        files[f"feeds/{feed.slug}.ics"] = calendar
    files["ufc.ics"] = files[f"feeds/{DEFAULT_FEED.slug}.ics"]
    log.info("Built and validated %d calendars for %d countries", len(files) - 1, len(countries))
    state = State(
        entries=entries,
        events={e.event_id: snapshot_event(scan.raw_events[e.event_id]) for e in upcoming},
        upcoming={e.event_id: e.start.isoformat() for e in upcoming},
        nationalities=nationalities,
        countries=countries,
        scan_from=min(
            (e.event_id for e in upcoming),
            default=scan.highest_id or previous.scan_from or args.seed_event_id,
        ),
        max_event_id=scan.highest_id,
        generated_at=now.isoformat(timespec="seconds"),
    )
    files["index.html"] = render_index(entries, countries, now).encode("utf-8")
    files["state.json"] = state.to_json().encode("utf-8")
    write_outputs(args.output_dir, files)
    write_job_summary(entries, changes, now)
    return entries


def read_previous_state(source: str | Path, session: requests.Session) -> State | None:
    """Load the previous state; None on the first run (no file / HTTP 404)."""
    source = str(source)
    if source.startswith(("https://", "http://")):
        try:
            response = session.get(source, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise FetchError(f"could not load the previous state from {source}: {exc}") from exc
        if response.status_code == 404:
            log.info("No previous state at %s; starting fresh", source)
            return None
        if response.status_code != 200:
            raise FetchError(f"previous state {source} returned HTTP {response.status_code}")
        text = response.content.decode("utf-8")
    else:
        path = Path(source)
        if not path.is_file():
            log.info("No previous state at %s; starting fresh", path)
            return None
        text = path.read_text(encoding="utf-8")
    return State.from_json(text)


def parse_events(raw_events: Mapping[int, dict]) -> list[Event]:
    events = []
    for event_id, raw in sorted(raw_events.items()):
        try:
            events.append(parse_event(raw))
        except ValueError as exc:
            log.warning("Skipping FightMetric event %s: %s", event_id, exc)
    return events


def cards_of(events: Iterable[Event]) -> dict[int, dict[int, set[int]]]:
    return {
        e.event_id: {
            f.fight_id: {x.fighter_id for x in f.fighters if x.fighter_id is not None}
            for f in e.fights
        }
        for e in events
    }


def check_missing_events(
    previous: State, current_ids: set[int], cutoff: datetime, allow_drop: bool
) -> None:
    """Stop when most events that were upcoming on the previous run are suddenly gone."""
    expected = {
        event_id
        for event_id, start in previous.upcoming.items()
        if (moment := parse_timestamp(start)) and moment >= cutoff
    }
    missing = sorted(expected - current_ids)
    if not missing:
        return
    message = f"{len(missing)} of {len(expected)} upcoming events disappeared: {missing}"
    if len(expected) >= 2 and len(missing) > len(expected) // 2 and not allow_drop:
        raise AbortError(f"{message} (use --allow-drop if this is real)")
    log.warning(message)


def check_cancellations(
    previous: Iterable[Entry], changes: Counter, cutoff: datetime, allow_drop: bool
) -> None:
    """Stop when more than half of the upcoming entries would be cancelled at once."""
    active = sum(
        1 for e in previous if e.status == CONFIRMED and not e.replaced_by and e.begins >= cutoff
    )
    cancelled = changes["cancelled"]
    if cancelled >= 3 and cancelled > active // 2 and not allow_drop:
        raise AbortError(
            f"{cancelled} of {active} upcoming entries would be cancelled "
            "(use --allow-drop if this is real)"
        )


def write_outputs(output_dir: Path, files: Mapping[str, bytes]) -> None:
    for name, data in files.items():
        path = output_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
    log.info("Wrote %d files to %s", len(files), output_dir)


def _upcoming(entries: Iterable[Entry], now: datetime) -> list[Entry]:
    return [e for e in entries if e.begins >= now - LOOKBACK]


def _when(entry: Entry) -> str:
    if entry.start:
        return datetime.fromisoformat(entry.start).astimezone(KST).strftime("%Y-%m-%d (%a) %H:%M")
    return f"{entry.day} (종일)"


def render_index(entries: Iterable[Entry], countries: Iterable[str], now: datetime) -> str:
    """The settings page: pick a feed, get its subscription URL and a preview."""
    upcoming = [e for e in _upcoming(entries, now) if e.status == CONFIRMED]
    counts = Counter(c for e in upcoming for c in e.countries)
    data = {
        "updated": now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
        "defaultCountry": DEFAULT_COUNTRY,
        "countries": sorted(
            (
                {"code": c, "name": country_name(c, korean=True), "flag": flag(c),
                 "upcoming": counts[c]}
                for c in countries
            ),
            key=lambda c: (c["code"] != DEFAULT_COUNTRY, -c["upcoming"], c["name"]),
        ),
        "entries": [
            {"when": _when(e), "matchup": e.matchup, "kind": e.kind, "main": e.main,
             "coMain": e.co_main, "women": e.women, "countries": e.countries}
            for e in upcoming
        ],
    }  # fmt: skip
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return INDEX_TEMPLATE.replace("__DATA__", payload)


def write_job_summary(entries: Iterable[Entry], changes: Counter, now: datetime) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    shown = [e for e in _upcoming(entries, now) if e.status == CONFIRMED]
    lines = [
        "## UFC calendar updated",
        "",
        "Changes: " + (", ".join(f"{k} {v}" for k, v in sorted(changes.items())) or "none"),
        "",
        f"Default feed (`ufc.ics`, {DEFAULT_FEED.slug}):",
        "",
        "| When (KST) | Entry |",
        "|---|---|",
    ]
    lines += [
        f"| {_when(e)} | {summary_for(e, DEFAULT_FEED).replace('|', '¦')} |"
        for e in shown
        if matches(DEFAULT_FEED, e)
    ]
    with open(path, "a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


INDEX_TEMPLATE = (Path(__file__).with_name("index.html")).read_text(encoding="utf-8")
