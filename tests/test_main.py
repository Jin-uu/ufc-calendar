import json

import pytest
import requests
from helpers import FakeResponse, FakeSession, fixture_session, fm_url
from icalendar import Calendar

from ufc_calendar import main as cli
from ufc_calendar.scraper import ESPN_SCOREBOARD_URL
from ufc_calendar.state import State

NOW = "2026-10-14T00:00:00+00:00"


def args(tmp_path, *extra):
    return cli.parse_args(
        ["--output-dir", str(tmp_path), "--now", NOW, "--delay", "0", "--seed-event-id", "1330",
         *extra]
    )  # fmt: skip


def summaries(tmp_path):
    calendar = Calendar.from_ical((tmp_path / "ufc.ics").read_bytes())
    return [str(e["SUMMARY"]) for e in calendar.walk("VEVENT")]


def run_cli(monkeypatch, session, tmp_path, *extra):
    monkeypatch.setattr(cli, "make_session", lambda: session)
    return cli.main(["--output-dir", str(tmp_path), "--now", NOW, "--delay", "0", *extra])


def test_end_to_end_with_fixtures(tmp_path):
    cli.run(args(tmp_path), fixture_session(1332, 1339, 1348))

    assert summaries(tmp_path) == [
        "🇰🇷 UFC | Cody Chovancek vs SuYoung You",
        "🥊 UFC Main | Joaquin Buckley vs Mike Malott",
        "🥊 UFC Co-Main | Erin Blanchfield vs Jasmine Jasudavicius",
        "🇰🇷 UFC | Julian Erosa vs JeongYeong Lee",
        "🥊 UFC Main | Renato Moicano vs Tom Nolan",
        "🥊 UFC Main | Jiri Prochazka vs Navajo Stirling",
        "🥊 UFC Co-Main | Aljamain Sterling vs Kevin Vallejos",
    ]
    state = State.from_json((tmp_path / "state.json").read_text())
    assert (state.scan_from, state.max_event_id) == (1332, 1348)
    assert set(state.upcoming) == {1332, 1339, 1348}
    assert "ufc.ics" in (tmp_path / "index.html").read_text()


def test_second_run_uses_previous_state(tmp_path):
    cli.run(args(tmp_path), fixture_session(1332, 1339))
    first = (tmp_path / "ufc.ics").read_bytes()
    session = fixture_session(1332, 1339)

    cli.run(args(tmp_path), session)

    assert (tmp_path / "ufc.ics").read_bytes() == first
    assert session.calls[0] == fm_url(1332)  # scan resumes at the first upcoming event


def test_espn_outage_falls_back_to_fightmetric_nationality(tmp_path):
    session = fixture_session(1332, espn=False)
    session.routes[ESPN_SCOREBOARD_URL] = FakeResponse(503, {})

    cli.run(args(tmp_path), session)

    assert "🇰🇷 UFC | Cody Chovancek vs SuYoung You" in summaries(tmp_path)


@pytest.mark.parametrize(
    "failure", [FakeResponse(503, {}), requests.ConnectionError("network down")]
)
def test_collection_failure_keeps_existing_calendar(monkeypatch, tmp_path, failure):
    assert run_cli(monkeypatch, fixture_session(1332), tmp_path) == 0
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    broken = fixture_session(1332)
    broken.routes[fm_url(1340)] = failure  # an id the previous run never saw

    assert run_cli(monkeypatch, broken, tmp_path) == 1

    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_known_event_failure_uses_previous_snapshot(tmp_path):
    cli.run(args(tmp_path), fixture_session(1332))
    session = fixture_session()
    session.routes[fm_url(1332)] = FakeResponse(500, {})

    cli.run(args(tmp_path), session)

    assert "🥊 UFC Main | Joaquin Buckley vs Mike Malott" in summaries(tmp_path)


def test_mass_disappearance_aborts_unless_allowed(monkeypatch, tmp_path):
    assert run_cli(monkeypatch, fixture_session(1332, 1339, 1348), tmp_path) == 0
    before = (tmp_path / "ufc.ics").read_bytes()

    assert run_cli(monkeypatch, fixture_session(1332), tmp_path) == 1
    assert (tmp_path / "ufc.ics").read_bytes() == before

    assert run_cli(monkeypatch, fixture_session(1332), tmp_path, "--allow-drop") == 0
    assert any(s.startswith("❌ 취소") for s in summaries(tmp_path))


def test_no_scheduled_events_is_not_a_failure(tmp_path):
    cli.run(args(tmp_path), FakeSession({ESPN_SCOREBOARD_URL: {"events": []}}))

    assert summaries(tmp_path) == []
    assert json.loads((tmp_path / "state.json").read_text())["entries"] == []


def test_previous_state_from_url(tmp_path):
    url = "https://example.github.io/ufc-calendar/state.json"
    published = tmp_path / "published"
    cli.run(args(published), fixture_session(1332))
    session = fixture_session(1332)
    session.routes[url] = FakeResponse(200, body=(published / "state.json").read_bytes())

    cli.run(args(tmp_path / "next", "--previous-state", url), session)

    assert session.calls[0] == url
    assert (tmp_path / "next" / "ufc.ics").read_bytes() == (published / "ufc.ics").read_bytes()


def test_previous_state_404_starts_fresh_but_other_errors_fail(monkeypatch, tmp_path):
    url = "https://example.github.io/ufc-calendar/state.json"
    session = fixture_session(1332)  # unknown non-FightMetric URLs answer 404

    assert run_cli(monkeypatch, session, tmp_path, "--previous-state", url) == 0

    session.routes[url] = FakeResponse(502, {})
    assert run_cli(monkeypatch, session, tmp_path / "other", "--previous-state", url) == 1
    assert not (tmp_path / "other").exists()
