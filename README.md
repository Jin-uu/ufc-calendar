# ufc-calendar

Builds iCalendar feeds of UFC bouts for subscription in Apple Calendar. Feeds are selected on a settings page by country, gender, main/co-main events and Road to UFC / Contender Series. GitHub Actions refreshes the data daily and GitHub Pages serves every feed at a fixed URL. No server or paid API is used.

```
Settings page: https://jin-uu.github.io/ufc-calendar/
Default feed:  https://jin-uu.github.io/ufc-calendar/ufc.ics   (Korean fighters + main/co-main)
```

### Feed settings

| Setting | Values | Effect |
|---|---|---|
| Country | none, or one country | Bouts with a fighter of that nationality |
| Main / co-main | both, main, co-main, none | Main and/or co-main events of every UFC event |
| Gender | all, men, women | Restricts the result (women = women's divisions, incl. catchweights between women) |
| Other events | on, off | Also takes the country's bouts from Road to UFC and Contender Series |

A bout is included if it matches the country or the main/co-main setting, and the gender setting. Each combination is a pre-built file `feeds/<country>-<gender>-<headline>-<rtu|ufc>.ics` (e.g. `feeds/jp-women-none-ufc.ics`). The page only assembles the URL; it does not generate data. One country per feed: subscribe to several feeds for several countries. Countries appear on the page once a fighter of that nationality has been collected and are never removed, so subscribed URLs stay valid. `ufc.ics` equals `feeds/kr-all-both-rtu.ics`.

| Title | Meaning |
|---|---|
| `🇰🇷 UFC \| A vs B` | Bout with a fighter of the selected country (flag of that country) |
| `🥊 UFC Main \| A vs B`, `🥊 UFC Co-Main \| A vs B` | Main / co-main event |
| `🇰🇷 UFC Main \| …` | Selected country's fighter in the main event |
| `🇰🇷 Road to UFC \| …`, `🇰🇷 DWCS \| …` | Country bout at Road to UFC / Contender Series |
| `❌ 취소 \| …` | Cancelled bout |

Start times are in KST and mark the start of the bout's card (main / prelims / early prelims).

## 1. Architecture

```mermaid
flowchart LR
    subgraph GA[GitHub Actions · daily 08:43 KST / manual]
        A[Load previous state.json<br/>from Pages] --> B[Collect UFC data<br/>FightMetric feed]
        B --> C[Validate<br/>abort on abnormal drop]
        C --> D[Enrich from ESPN<br/>nationality, card times]
        D --> E[Collect all bouts<br/>country, gender, main, co-main]
        E --> F[Merge with previous master entries<br/>UIDs, changes, cancellations]
        F --> G[Build + validate one .ics per feed]
    end
    G --> H[GitHub Pages<br/>feeds/*.ics, ufc.ics, index.html, state.json]
    H --> I[Apple Calendar]
    H -. next run .-> A
```

| Module | Responsibility |
|---|---|
| [`scraper.py`](src/ufc_calendar/scraper.py) | HTTP with timeout/retry, FightMetric discovery and parsing, ESPN matching |
| [`models.py`](src/ufc_calendar/models.py) | `Event`, `Fight`, `Fighter` |
| [`countries.py`](src/ufc_calendar/countries.py) | IOC codes / ESPN names → ISO alpha-2, names, flags |
| [`filter.py`](src/ufc_calendar/filter.py) | Feed definitions, feed matching, main and co-main detection |
| [`schedule.py`](src/ufc_calendar/schedule.py) | Timestamp parsing, KST conversion, card start selection |
| [`state.py`](src/ufc_calendar/state.py) | Master entries across runs: UIDs, SEQUENCE, cancellation, replacement |
| [`ics.py`](src/ufc_calendar/ics.py) | Per-feed titles and selection, RFC 5545 output and validation |
| [`main.py`](src/ufc_calendar/main.py) | CLI, pipeline, failure handling, output |
| [`index.html`](src/ufc_calendar/index.html) | Settings page template |

The ICS module is named `ics.py` to avoid shadowing the standard library `calendar` module.

Output (`output/` locally, `public/` in Actions):

- `feeds/*.ics`: one calendar per setting combination (9 + 24 per country, about 800 files, ~7 MB).
- `ufc.ics`: the default feed.
- `index.html`: settings page with subscription link and preview.
- `state.json`: state read by the next run (master entries, UIDs, event snapshots, nationality cache, countries). Public fight data only.

Every collected bout is one master entry carrying its attributes (countries, gender, main, co-main, event type). UIDs and cancellations are tracked once on the master entries; each feed is a filtered view, so a bout has the same UID in every feed. The previous state is read back from GitHub Pages, so the workflow needs no repository write access.

## 2. Data sources

| Source | Free | Nationality | Main/co-main | Card times | Usable from Actions | Use |
|---|---|---|---|---|---|---|
| UFC FightMetric feed (`d29dxerjsp82wz.cloudfront.net/api/v3/event/live/{id}.json`) | Yes, no key | Birthplace only | Official bout order | Per card | Yes | Primary |
| ESPN scoreboard JSON (`site.api.espn.com/.../mma/ufc/scoreboard`) | Yes, no key | Flag (matches ufc.com) | Not explicit | Per bout | Yes | Secondary |
| ufc.com HTML | Yes | Yes | Yes | Yes | No (403 bot protection) | Excluded; not bypassed |
| UFCStats / Tapology / Sherdog | — | Missing or ToS-restricted | — | No | — | Excluded |

**FightMetric (primary).** The JSON behind ufc.com and the UFC app. Provides a unique `FightId` (used for UIDs), `FightOrder` (1 = main event, 2 = co-main), `CardSegment` (Main / Prelims1 / Prelims2) with UTC start times, weight class, title status and venue.

**ESPN (secondary).** FightMetric has no nationality field, only `Born` and `FightingOutOf` (e.g. Mike Malott: born USA, Canadian). ESPN flags match ufc.com and take precedence. ESPN per-bout card times fill bouts that FightMetric has not yet assigned to a card. One ESPN request per run.

**Limitations**

- Both endpoints are undocumented and may change without notice. Responses are validated; anomalies abort deployment. Request volume is low (tens per day).
- FightMetric has no event listing. Event ids are sequential, so the scan starts at the smallest upcoming id from the previous run and stops after 10 consecutive unused ids (about 30–40 requests per run, 0.25 s apart).
- Nationality fallback order: ESPN → cached ESPN value → FightMetric birthplace. Names are never used to infer nationality. All spellings are normalised to ISO 3166-1 alpha-2 (FightMetric uses IOC codes, e.g. `BRN` = Bahrain).

## 3. Local usage

Requires macOS or Linux and [uv](https://docs.astral.sh/uv/). uv installs Python 3.12.

```bash
git clone https://github.com/Jin-uu/ufc-calendar.git
cd ufc-calendar
uv sync
uv run ufc-calendar
```

| Option | Description |
|---|---|
| `--output-dir DIR` | Output directory (default `output`) |
| `--previous-state PATH_OR_URL` | Previous `state.json` (default `<output-dir>/state.json`) |
| `--days N` | Collection window (default 90) |
| `--allow-drop` | Publish despite mass disappearance of events or entries |
| `--seed-event-id N` | First event id to scan without previous state (default 1330) |
| `-v` | Debug logging |

Re-running in the same directory keeps UIDs. Unchanged data yields a byte-identical `ufc.ics`.

## 4. Tests

```bash
uv run pytest
```

Tests use trimmed real responses (`tests/fixtures/`) and a fake HTTP session; no network access is required.

| File | Coverage |
|---|---|
| `test_scraper.py` | Parsing, missing fields, HTTP/network/JSON errors, unconfirmed cards, id scan, snapshot fallback, ESPN matching |
| `test_filter.py` | Country / headline / gender / side-event settings, feed slugs, main/co-main detection, missing nationality, ambiguous order |
| `test_countries.py` | IOC / ISO / name normalisation, names, flags |
| `test_schedule.py` | UTC → Asia/Seoul, US DST, per-card start, missing-time fallbacks |
| `test_ics.py` | Validity, per-feed titles and names, cancelled-copy visibility, escaping, Korean/emoji, 75-octet folding, determinism |
| `test_state.py` | UID stability (same bout, time/date change, replacement, re-created bout), cancelled copies, no duplicates, cancel/restore/remove, history |
| `test_main.py` | End-to-end incl. feed files and settings page data, failure preserves output, abnormal-drop abort, ESPN outage, remote previous state |

## 5. GitHub Actions

Workflow: [`.github/workflows/update-calendar.yml`](.github/workflows/update-calendar.yml)

- Schedule: 23:43 UTC (08:43 KST). Scheduled runs often start late, so updates land around 09:00 KST.
- Manual: Actions → *Update UFC calendar* → *Run workflow*. Enable `allow_drop` to override the safety checks.

Steps: install dependencies (`uv sync --locked`) → `pytest` → load previous `state.json` from Pages (404 = first run) → collect, validate, enrich, filter, build → validate ICS → `actions/upload-pages-artifact` → `actions/deploy-pages`.

| Condition | Behavior |
|---|---|
| Network/HTTP error, malformed response | Timeout 5/20 s; up to 3 retries on 429/5xx with backoff. Known events fall back to the previous snapshot; otherwise the run fails without deploying. |
| ESPN unavailable | Warning; continues with cached / birthplace nationality. |
| More than half of previously upcoming events missing | Abort (override: `allow_drop`). |
| More than half (≥ 3) of upcoming entries cancelled at once | Abort (override: `allow_drop`). |
| ICS validation failure | Abort. |
| Collection succeeds with no upcoming events | Deploy; history retained. |

A failed run skips deployment, so Pages keeps the last good `ufc.ics`. Errors appear in the log and as `::error` annotations; successful runs write a job summary.

Permissions: `contents: read` for the workflow; `pages: write` and `id-token: write` for the deploy job only. No personal access token.

## 6. GitHub Pages setup

1. Repository → Settings → Pages.
2. Build and deployment → Source: **GitHub Actions**.
3. Actions → *Update UFC calendar* → *Run workflow*.
4. Verify `https://jin-uu.github.io/ufc-calendar/` and `https://jin-uu.github.io/ufc-calendar/ufc.ics`.

GitHub Pages on the free plan requires a public repository.

## 7. Apple Calendar subscription

Choose the settings on `https://jin-uu.github.io/ufc-calendar/`, then either press **Apple 캘린더에서 구독** (opens a `webcal://` link) or copy the URL.

### macOS

1. Calendar → File → New Calendar Subscription.
2. Enter the feed URL → Subscribe.
3. Location: **iCloud** (syncs to iPhone). Auto-refresh: daily or hourly. The feed contains no alerts.

### iOS / iPadOS

Not required if subscribed via iCloud on macOS.

- Open the settings page in Safari, choose the settings and tap **Apple 캘린더에서 구독**, or
- Settings → Apps → Calendar → Calendar Accounts → Add Account → Other → Add Subscribed Calendar (iOS 17 and earlier: Settings → Calendar → Accounts), then enter the feed URL.

Subscribed calendars are read-only. Refresh timing is controlled by the device; iCloud subscriptions may lag by hours.

## 8. Updates and cancellations

UIDs derive from the FightMetric `FightId` (`fight-13132@ufc-calendar`), never from names or times.

| Change | Handling |
|---|---|
| Time or date change | Same UID; time updated, `SEQUENCE` incremented, `LAST-MODIFIED` updated |
| Fighter replaced, same `FightId` | Same UID; title and description updated |
| Fighter replaced, new `FightId` | Inherits the UID of a vanished bout in the same event sharing a fighter |
| Bout cancelled, removed, or event deleted | Prefixed `❌ 취소 \|`, `STATUS:CANCELLED`; removed one day after the original time |
| Fighter leaves a bout (replacement) | The bout keeps its UID; a cancelled copy of the old version is shown only in feeds where the current version no longer appears (e.g. the country feed of the replaced fighter) |
| Bout no longer matches a feed (e.g. no longer co-main) | Disappears from that feed |
| Bout finished | Kept unchanged for one year; results not shown |

Start time precedence (the source is stated in the description; entries last one hour):

1. Official start of the bout's card.
2. Official start published for another bout on the same card.
3. ESPN card start for the bout.
4. Event start, only if FightMetric provides the local time zone; marked as a fallback.
5. All-day entry.

Source timestamps are UTC with offsets and are converted to `Asia/Seoul`, so US DST transitions are handled correctly. The ICS includes `TZID=Asia/Seoul` and a `VTIMEZONE`.

Main/co-main: bout order 1 is the main event, 2 the co-main. Duplicate or missing order yields neither. The co-main must be on the main card; for unassigned bouts, it is accepted only if ESPN splits the event into several card times and the bout shares the main event's time.

## 9. Known limitations

- Undocumented APIs. Format changes stop updates; the existing calendars are preserved.
- One country per feed (combinations are pre-built). Several countries require several subscriptions; a bout between two selected countries then appears in both calendars.
- Event discovery depends on sequential ids. A change in id allocation may require adjusting `--seed-event-id`.
- Fighters absent from ESPN are classified by birthplace; dual nationals or foreign-born fighters may be misclassified. Undeterminable fighters are logged and match no country.
- Newly announced cards often lack segment assignments. Times then come from ESPN, and co-mains may appear late.
- Fight Night URLs are built from the local date (`ufc.com/event/ufc-fight-night-<month>-<dd>-<yyyy>`) and may occasionally differ. Road to UFC and DWCS link to the events page.
- Propagation delay: Actions scheduling, Pages CDN cache (~10 min), and Apple Calendar refresh interval.
- GitHub disables scheduled workflows in public repositories after 60 days without repository activity. Re-enable from the Actions tab or push a commit. No auto-commit workaround is included, to avoid granting write access.
- `state.json` is public and contains public fight data only.
