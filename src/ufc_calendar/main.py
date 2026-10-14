"""Command line entry point: collect, filter, build, validate and write the calendar.

Nothing is written unless every step succeeds, so a failed run leaves the previously published
calendar untouched.
"""

from __future__ import annotations

import argparse
import html
import logging
import os
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from .filter import select_fights
from .ics import CalendarValidationError, build_calendar, draft_for, validate_calendar
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
        description="Build an iCalendar feed of UFC bouts with Korean fighters and headliners.",
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

    # 3. Filter and merge with the previous calendar.
    drafts = [d for d in map(draft_for, select_fights(window)) if d]
    entries, changes = reconcile(previous.entries, drafts, cards_of(events), now, cutoff)
    log.info("Calendar changes: %s", dict(sorted(changes.items())))
    check_cancellations(previous.entries, changes, cutoff, args.allow_drop)

    # 4. Build, validate and write.
    calendar = build_calendar(entries)
    log.info("ufc.ics is valid: %d events", validate_calendar(calendar))
    state = State(
        entries=entries,
        events={e.event_id: snapshot_event(scan.raw_events[e.event_id]) for e in upcoming},
        upcoming={e.event_id: e.start.isoformat() for e in upcoming},
        nationalities=nationalities,
        scan_from=min(
            (e.event_id for e in upcoming),
            default=scan.highest_id or previous.scan_from or args.seed_event_id,
        ),
        max_event_id=scan.highest_id,
        generated_at=now.isoformat(timespec="seconds"),
    )
    write_outputs(args.output_dir, calendar, state, render_index(entries, now))
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
    active = sum(1 for e in previous if e.status == CONFIRMED and e.begins >= cutoff)
    cancelled = changes["cancelled"]
    if cancelled >= 3 and cancelled > active // 2 and not allow_drop:
        raise AbortError(
            f"{cancelled} of {active} upcoming entries would be cancelled "
            "(use --allow-drop if this is real)"
        )


def write_outputs(output_dir: Path, calendar: bytes, state: State, index_html: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "ufc.ics": calendar,
        "index.html": index_html.encode("utf-8"),
        "state.json": state.to_json().encode("utf-8"),
    }
    for name, data in files.items():
        temporary = output_dir / f".{name}.tmp"
        temporary.write_bytes(data)
        temporary.replace(output_dir / name)
    log.info("Wrote %s", ", ".join(str(output_dir / name) for name in files))


def _upcoming(entries: Iterable[Entry], now: datetime) -> list[Entry]:
    return [e for e in entries if e.begins >= now - LOOKBACK]


def _when(entry: Entry) -> str:
    if entry.start:
        return datetime.fromisoformat(entry.start).astimezone(KST).strftime("%Y-%m-%d (%a) %H:%M")
    return f"{entry.day} (종일)"


def render_index(entries: Iterable[Entry], now: datetime) -> str:
    rows = (
        "\n".join(
            f"<li><time>{html.escape(_when(e))}</time> {html.escape(e.summary)}</li>"
            for e in _upcoming(entries, now)
        )
        or "<li>예정된 경기가 없습니다.</li>"
    )
    updated = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    return INDEX_TEMPLATE.format(rows=rows, updated=updated)


def write_job_summary(entries: Iterable[Entry], changes: Counter, now: datetime) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## UFC calendar updated",
        "",
        "Changes: " + (", ".join(f"{k} {v}" for k, v in sorted(changes.items())) or "none"),
        "",
        "| When (KST) | Entry |",
        "|---|---|",
    ]
    lines += [f"| {_when(e)} | {e.summary.replace('|', '¦')} |" for e in _upcoming(entries, now)]
    with open(path, "a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


INDEX_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UFC 주요 경기 캘린더</title>
<style>
  :root {{ color-scheme: light dark; --fg: #1d1d1f; --muted: #6e6e73; --line: #d2d2d7;
          --bg: #fff; --accent: #d20a0a; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg: #f5f5f7; --muted: #a1a1a6; --line: #38383a; --bg: #111; }}
  }}
  body {{ margin: 0 auto; max-width: 720px; padding: 24px 16px; color: var(--fg);
         background: var(--bg);
         font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", sans-serif; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 4px; }}
  .muted {{ color: var(--muted); font-size: .9rem; }}
  code {{ word-break: break-all; }}
  a.button {{ display: inline-block; margin: 12px 0; padding: 10px 16px; border-radius: 8px;
             background: var(--accent); color: #fff; text-decoration: none; font-weight: 600; }}
  ul {{ padding-left: 0; list-style: none; }}
  li {{ padding: 6px 0; border-bottom: 1px solid var(--line); }}
  time {{ display: block; color: var(--muted); font-size: .85rem; }}
</style>
</head>
<body>
<h1>UFC 주요 경기 캘린더</h1>
<p class="muted">한국 선수 출전 경기 · UFC 메인/코메인 이벤트 · 매일 자동 갱신
(마지막 갱신 {updated})</p>
<p>구독 URL: <code id="url">ufc.ics</code></p>
<a class="button" id="subscribe" href="ufc.ics">Apple 캘린더에서 구독</a>
<p class="muted">시간은 개별 경기가 아닌 해당 카드(메인/프릴림)의 시작 시간입니다.</p>
<h2>예정된 경기</h2>
<ul>
{rows}
</ul>
<script>
  const ics = new URL("ufc.ics", location.href);
  document.getElementById("url").textContent = ics.href;
  document.getElementById("subscribe").href = ics.href.replace(/^https?:/, "webcal:");
</script>
</body>
</html>
"""
