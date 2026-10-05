# reinvent-planner

Plan your AWS re:Invent schedule from the terminal: search the whole catalog, rank what you
want, catch clashes and impossible walks between venues, print a reservation checklist, and
export calendar files that update in place.

It uses the **official [AWS Events API](https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html)**
and signs you in with your own AWS Builder ID. No copying cookies out of your browser.

![The app: search, a day's timeline and Strip map, and the reservation-day countdown](docs/demo.gif)

<sub>Recorded with made-up sessions (`scripts/demo/demo.tape`).</sub>

> **Not affiliated with or endorsed by Amazon Web Services.** "AWS" and "re:Invent" are
> trademarks of Amazon.com, Inc. or its affiliates. This is an independent open-source tool.

## Install

You only need [uv](https://docs.astral.sh/uv/). It downloads a suitable Python by itself, so
there is nothing else to install.

```bash
brew install uv          # or see https://docs.astral.sh/uv/getting-started/installation/
uv tool install git+https://github.com/ravitej1993/reinvent-planner    # PyPI release coming soon
```

Once it's on PyPI this becomes `uv tool install reinvent-planner`, or run it without
installing: `uvx reinvent-planner setup`.

This installs two identical commands: `reinvent-planner` and the short `rip`. (If you use the
`rip` file-deletion tool, stick with `reinvent-planner`.)

## Quick start

```bash
rip setup                        # sign in with AWS Builder ID, download the catalog, show next steps
```

Then:

```bash
rip search agents --level 300 --day tue --type workshop
rip show AIM301
rip fav add AIM301 AIM302        # asks before changing your schedule
rip rank set AIM301 1 --backup AIM302
rip time add "Team lunch" --day tue --start 12:00 --end 13:00 --where "Wynn buffet"
rip plan                         # by-day agenda with overlaps and tight transfers flagged
rip checklist --out checklist.md # ranked list to reserve from
rip ics                          # calendar files in ./calendar
rip csv                          # your plan as a spreadsheet
```

Everything defaults to `reinvent2026`. Use `--event <id>` (or `RIP_EVENT`) for other events;
`rip events` lists them.

## The interactive app

Prefer clicking to typing? The same planner comes as an app, in two forms:

```bash
rip tui      # in this terminal window
rip ui       # in a browser tab (runs only on your computer; nothing is hosted)
```

It does everything the commands do: search with the same filters, session details, ranking,
favorites, cancelling a reservation, personal time, your plan, the reservation checklist, the
calendar feed, exports, travel-time corrections and signing out. Anything that changes your
schedule asks first, as the commands do. Keys: `s` search, `p` plan, `c` checklist, `t`
personal time, `l` launch, `m` more, `/` jump to the search box, Escape to leave a box, `r`
refresh your schedule, `q` quit. It uses the same sign-in, catalog and ranking as the
commands, so you can switch between them freely.

- **My plan** shows each day as a timeline (overlaps stack into lanes, and travel between
  venues shows as ░, in red when it's too tight) and as a map of the Strip with your route,
  each leg labelled with its walk.
- **Launch** is for reservation day. Set your target time and it counts down, in the event's
  time and yours, corrected to the API's clock.
  - *Website copilot* (for Oct 6, when only the website takes reservations): it shows one
    pick at a time while you reserve on the website. Press `b` booked, `f` full (moves to the
    backup) or `x` skip, and check against your real schedule when you like. It never
    changes anything.
  - *API launch* (from Oct 8): arming shows the plan and checks your sign-in. At your target
    time GO lights up, and pressing it runs `rip reserve` with a live board of each pick:
    booked, full, falling back to a backup.
  - Fair use is built in. Nothing fires by itself, polls, or retries full sessions. One press
    is one run, followed by a one-minute cooldown (longer after a "not open yet"), shared
    with `rip reserve`. Only one run can happen at a time on your computer. If a run is cut
    off, the next start tells you what went through.
  - *Hide email* keeps your address out of screenshots.
- **Personal time** lists your blocks (lunches, meetings) with Add, Edit and Remove; times
  are entered in the event's time zone and checked before anything is sent.
- **More** holds the calendar feed (publish, show the link, unpublish), downloads (your plan
  or the whole catalog as CSV, and calendar files; in the browser they arrive as normal
  downloads, in the terminal they're saved to your Downloads folder), your travel-time
  corrections, and signing out.
- The theme is `rip-neon`. Set `RIP_THEME` to any Textual theme (for example
  `textual-light`) to change it, or pick one from the command palette (`ctrl+p`).

`rip ui` starts a small server on `127.0.0.1` and opens a one-time link in your browser; the
page works only in that browser, only on this computer, and only until you press Ctrl+C. Use
`--no-browser` to print the link instead, or `--port N` to pick the port. On a Mac,
`scripts/reinvent-planner-ui.command` starts it with a double-click.

## Commands

| Command | What it does |
|---|---|
| `setup` | First run: sign in if needed, download the catalog, show what to do next |
| `login` / `logout [--builder-id]` / `whoami` | Sign in with Builder ID; revoke and delete tokens; show who is signed in |
| `events [--past]` | List events available through the API |
| `sync [--force]` | Download the full catalog; report new, removed and changed sessions, and your picks that are filling up |
| `search [WORDS]` | Full-text search with the website's filters (`--type --level --feature --topic --area --industry --role --service --venue --favorites`) plus `--day --reserved --laptop --reservable --available` |
| `venues` / `venues set A B MIN` / `venues reset [A B]` (all of them asks first) / `venues measure [--force]` | Travel time to allow between venues (walk, monorail tip, 2025 shuttles and connections); correct a figure after trying it on site; measure a table for another multi-venue event |
| `show CODE` | One session in full |
| `schedule` | Your reservations, favorites and personal time by day, with problems flagged |
| `fav add CODE…` / `fav rm CODE` / `fav ls` | Manage favorites on your real event schedule; warns about clashes and walks you can't make, confirms, then reads the schedule back |
| `rank set CODE N [--backup CODE]…` / `rank rm` / `rank ls` | Your private ranking, stored locally only; warns about clashes and walks you can't make |
| `time add TITLE --day D --start HH:MM --end HH:MM [--where W] [--note TEXT]` / `time ls` / `time edit REF [--title --day --start --end --where --note]` / `time rm REF` | Personal time (lunches, meetings) on your real event schedule, in the event's local time; warns about clashes and asks first. `REF` is the number from `time ls` or the block's ID (with `--yes`, only the ID: numbers can shift) |
| `plan` | Everything above in one agenda, with overlaps and too-tight venue transfers |
| `checklist [--out FILE]` | Picks in rank order: which to reserve, which are already done, which only matter if a higher pick fails |
| `reserve [CODE…] [--dry-run]` | Reserve your ranked picks in order, falling back to backups when one is full; confirms first and reads your schedule back |
| `cancel CODE` | Cancel one reservation, freeing the seat |
| `csv [--all] [--out FILE]` | Your plan (or the whole catalog) as a spreadsheet for Excel, Sheets or Numbers; `--out -` prints it |
| `calendar publish [--personal] [--ranked] [--no-auto]` / `calendar url` / `calendar unpublish` | A calendar feed that updates itself: your plan in a secret GitHub Gist to subscribe to from Google, Apple or Outlook (see below) |
| `tui` / `ui [--no-browser] [--port N]` | The interactive app, in the terminal or in a local browser tab (see above) |
| `ics [--split-by-type]` | Calendar files with stable IDs: Apple Calendar, Outlook and calendar subscriptions update events in place (Google's one-off import skips ones it already has) |

## A calendar that updates itself

`rip ics` writes files you import once. To have your calendar follow changes, publish a feed:

```bash
rip calendar publish       # asks first, then prints a link to subscribe to
```

- **Where it goes:** a *secret* GitHub Gist on the account your [GitHub CLI](https://cli.github.com)
  (`gh`) is signed into (always github.com, never an Enterprise server). It isn't listed or
  searchable, but **anyone with the link can read it, including its revision history** (every
  earlier version of your schedule), so treat the link like a password. This tool never sees a
  GitHub token; it asks `gh` to do the upload.
- **What's in it:** your reserved sessions and favorites. Personal time (`--personal`) and your
  private ranking (`--ranked`) are left out unless you ask, and widening what's shared asks
  again. Sharing *less* starts a fresh gist with a new link and deletes the old one, history
  and all.
- **Keeping it current:** after `rip reserve`, `cancel`, `fav` and `time` changes the feed updates
  itself, and so does any command that downloads your schedule (`rip sync`, `rip schedule`, …) when
  it finds a change, such as sessions you reserved on the event website or a session that moved to
  another time or room. It compares against what it last published, so it updates exactly when
  what subscribers see would change (never for nothing), and retries after a failed update. This
  means read commands may take a moment to update the gist; `--offline` skips that. (Turn
  automatic updates off with `--no-auto`, back on with `--auto`.) Apple Calendar and Outlook re-fetch about hourly;
  Google Calendar refreshes subscribed calendars on its own schedule, typically every 12–24 hours.
- **Subscribing:** Google Calendar → Other calendars → **+** → From URL. Apple Calendar →
  File → New Calendar Subscription (use the `webcal://` link it prints). Outlook → Add calendar →
  Subscribe from web.
- **If the gist disappears** (deleted on GitHub, or `gh` signed into another account), automatic
  updates pause instead of creating anything; `rip calendar publish` sets it up again and asks
  first.
- **Stopping:** `rip calendar unpublish` deletes the gist and its history.

## How full is a session?

The official API doesn't give seat counts or room capacity, only a band: available, limited,
very limited, full, or walk-up. (Before reservations open, it gives none at all.) Once bands
appear, every `rip sync` compares them with the previous sync and lists **sessions on your list
that are filling up**, e.g. "Available → Very limited", so you know what to reserve first.
Tools that show exact capacity get it by scraping the website with your browser cookies, which
this tool deliberately doesn't do.

## Common options

| Option | Where | What it does |
|---|---|---|
| `--event ID`, `-e` (or `RIP_EVENT`) | every command | Work on another event; `rip events` lists them |
| `--offline` | `search`, `schedule`, `plan`, `checklist`, `csv`, `ics` | Use your last saved schedule instead of asking the API (handy on conference Wi-Fi) |
| `--yes`, `-y` | `fav add`, `fav rm`, `reserve`, `cancel`, `time add/edit/rm`, `calendar publish/unpublish`, `venues reset`, `venues measure` | Skip the confirmation prompt |
| `--dry-run` | `reserve` | Show what would be reserved; change nothing |
| `--limit N`, `-n` | `search` | Maximum results (default 50; `0` for all) |
| `--out FILE`, `-o` / `--out-dir DIR` | `checklist`, `csv` / `ics` | Where to write files (`csv -o -` prints to the terminal). A file's folder must already exist; `ics` creates its folder. An existing file keeps its permissions, a new one is readable only by you |
| `--no-browser` | `login`, `setup` | Print the sign-in link instead of opening a browser |
| `--no-abstracts` | `sync` | Skip session descriptions for a smaller, faster download |
| `--force` | `sync` | Replace the local catalog even if the new one is much smaller |

## Reservations: important dates for re:Invent 2026

- **Oct 6** – reserved seating opens **on the event website**.
- **Oct 8** – reserving opens **through the API**. Until then, API reservations return `409`.

So the website opens two days before any API tool can reserve. Plan and rank *before* Oct 6,
then reserve on the website from `rip checklist`. From Oct 8, `rip reserve` picks up the rest:

```bash
rip reserve --dry-run   # what would be tried, in order; changes nothing
rip reserve             # asks, then reserves
```

How `rip reserve` works:

1. It reads your schedule, then takes each pick in rank order: the pick itself, or its first
   backup that fits around everything you already hold (including personal time).
2. It reserves that set in one request, top picks first, then reads your schedule back. The API
   answers per session, so the readback decides what actually succeeded.
3. A refused pick (full, not in your pass, clashing) falls through to its backup. A lower pick
   that clashed with a failed higher one gets its turn. This repeats until nothing new fits.

If the outcome of a request is unknown (a timeout or server error), it reads your schedule and
re-sends only what's missing, at most once. A pick that still can't be confirmed is reported as
"may or may not be reserved", and its backups are *not* tried, so you can't end up holding both;
check `rip schedule`. Before reservations open it reports "not open" and changes nothing, so it's
safe to try early.

Only one run can happen at a time on your computer (`rip reserve` and the app's Launch tab share
a lock), and after a run there's a one-minute pause before the next (longer after repeated "not
open" answers). If a run is cut off, for example by closing the terminal, the next `rip reserve`
tells you what it had sent and what's on your schedule now.

## Security model

This tool acts on your event schedule, so it's built to deserve that trust:

- **Official OAuth sign-in with PKCE.** You sign in on AWS's own page. The tool never sees your
  password. The client ID is public and shared by design; there is no secret to leak.
- **Local only.** The sign-in callback listens on `127.0.0.1` (ports 8485–8489, then 8484) and
  ignores any request that doesn't carry the random `state` value of the sign-in it started, so
  other programs or web pages can't end or spoof a sign-in. Idle connections time out, and on
  Windows the port is claimed exclusively. Nothing about you leaves your machine except calls to
  AWS (and GitHub, if you publish a calendar feed).
- **The browser app is locked to you.** `rip ui` listens on `127.0.0.1` only, so other devices
  can't reach it. It opens with a random one-time link that sets a private cookie; every page,
  file and connection needs that cookie, requests must be addressed to exactly
  `127.0.0.1:<port>` (which stops other websites reaching it through DNS tricks), and live
  connections from any other page are refused. The page loads nothing from the internet and
  sends strict browser security headers. The link is never written to logs.
- **Tokens in your OS keychain** (macOS Keychain, Windows Credential Manager, Secret Service). New
  tokens are written to a spare slot before switching over, so a failed save never loses a working
  sign-in, and two `rip` processes refreshing at once can't sign each other out. On a machine with
  no keychain you can opt in to `RIP_TOKEN_STORE=file`, which writes a file only your user can read
  (created fresh each time, never through a symlink) and refuses to load it if its permissions are
  loosened, if it's a symlink, or if another user owns it.
- **Your access token only goes to `api.awsevents.com`,** enforced in code for every request, with
  redirects disabled; the refresh token goes only to `oauth.awsevents.com` (to refresh it, and to
  revoke it on `rip logout`). Tokens are never logged or put in URLs, and crash tracebacks never
  print variables.
- **Short-lived access.** Access tokens last 60 minutes and refresh automatically. `rip logout`
  revokes the refresh token; `--builder-id` also ends your Builder ID browser session.
- **Your data stays local.** The catalog, your cached schedule and your ranking live in a SQLite
  file in your user data directory with owner-only permissions. No telemetry.
- **Writes are deliberate.** Every change to your schedule asks first (`--yes` to skip), and the tool
  reads your schedule back afterwards, because the API reports success per session. A write whose
  outcome is unknown is never re-sent blindly.
- **Safe exports and output.** CSV exports neutralize anything a spreadsheet would run as a formula,
  and text from the API or your own config files can't inject terminal markup or escape sequences:
  control characters are stripped as soon as data arrives. Files are written whole or not at all,
  and never through a symlink.

Found a vulnerability? See [SECURITY.md](SECURITY.md).

## Fair use

The API applies per-attendee quotas (for example, 30 reservations per minute). This tool honors
`Retry-After` on `429`, never re-sends a write whose outcome is unknown without checking your
schedule first, and acts only when you run a command: `rip reserve` does a bounded number of
rounds and then exits. It has no background seat-sniping, no polling for seats that open up, and
no multi-account features. A tool that
behaves this way is good for everyone at the event, and keeps the API open to tools like this.

## Where things are stored

| What | macOS | Linux | Windows |
|---|---|---|---|
| Catalog, schedule cache, ranking | `~/Library/Application Support/reinvent-planner/` | `~/.local/share/reinvent-planner/` | `%LOCALAPPDATA%\reinvent-planner\` |
| Travel-time overrides, file token store | `~/Library/Application Support/reinvent-planner/` | `~/.config/reinvent-planner/` | `%APPDATA%\reinvent-planner\` |

Override with `RIP_DATA_DIR` and `RIP_CONFIG_DIR`.

## Travel times between venues

`fav add`, `rank set`, `plan`, `checklist` and the `rip reserve` preview warn about
back-to-back sessions you can't comfortably reach in time. **Travel never stops a reservation**:
re:Invent runs on a 30-minute grid, so most back-to-back sessions in different venues are tight,
and whether to make the dash is your call. Only sessions that actually overlap are refused.
`rip venues` shows the table:

| Allow (minutes) | Venetian | Wynn | Encore | Caesars Forum | Caesars Palace | MGM Grand |
|---|---|---|---|---|---|---|
| **Venetian** | — | 35 | 35 | 25 | 45 | 45 |
| **Wynn** | 35 | — | 20 | 35 | 45 | 45 |
| **Encore** | 35 | 20 | — | 35 | 45 | 45 |
| **Caesars Forum** | 25 | 35 | 35 | — | 35 | 40 |
| **Caesars Palace** | 45 | 45 | 45 | 35 | — | 45 |
| **MGM Grand** | 45 | 45 | 45 | 40 | 45 | — |

How the figures were made:

- **Walking (measured):** street routes between the venues, from OpenStreetMap (e.g. Venetian ↔
  Caesars Forum is 0.9 km (0.6 mi), 12 minutes on foot; Encore ↔ MGM Grand is 3.4 km (2.1 mi),
  45 minutes). `rip venues` and the warnings show distances in both kilometres and miles.
- **Allow** = that walk plus 10 minutes (an assumption) to get between the street and a room
  inside the resort, rounded up to 5 and capped at 45 (take a shuttle beyond that).
- **Monorail (an off-peak tip):** the walks to and from the stations are measured; boarding
  (5 minutes; trains every 4–8 minutes) and the ride (matching the official 15 minutes end to
  end) are modelled. It clearly beats walking only for **Caesars Forum ↔ MGM Grand**: about
  24 minutes, 13 of them walking, against a 27-minute walk. At the :00/:30 rush and after keynotes
  boarding takes longer, so allowances stay walking-based and the monorail shows up as a tip in
  warnings and `rip venues`. According to
  [Splunk's re:Invent guide](https://www.splunk.com/en_us/about-us/events/aws-reinvent.html), the
  badge covered the monorail in 2025 (MGM Grand, Horseshoe/Paris, Flamingo/Caesars Palace and
  Harrah's/The LINQ stations); check the 2026 attendee guide.

The catalog lists Wynn and Encore together as "Wynn/Encore"; the room name tells them apart
(Wynn's Convention Promenade rooms are named after wines, Encore's after composers), and they're
20 minutes apart. Map data doesn't cover casino floors or indoor bridges, so treat the figures
as good estimates. Everything is in
[`data/reinvent2026.toml`](src/reinvent_planner/data/reinvent2026.toml).

**On site:** `rip venues` also shows where re:Invent shuttles ran in 2025 ("between most
properties", except Venetian ↔ Wynn/Encore and Venetian ↔ Caesars Forum; "?" where the guide
doesn't say, such as Caesars Palace) and the walking connections
(a pedestrian bridge between Wynn and the Venetian, a temporary Caesars Forum ↔ Venetian walkway
during Expo hours, Wynn and Encore joined indoors), per
[Splunk's 2025 guide](https://www.splunk.com/en_us/about-us/events/aws-reinvent.html); check the
2026 attendee guide. When a figure turns out wrong on day 1, correct it:

```bash
rip venues set Venetian Wynn 25     # saved in <config dir>/venues/reinvent2026.local.toml
rip venues reset Venetian Wynn      # or `rip venues reset` for all
```

Corrections are marked `*` and layer over the bundled table; the small file is plain text, so
teammates can share theirs; a correction naming a venue the table doesn't know is reported, not
silently dropped. (To replace the whole table, put a complete file at
`<config dir>/venues/reinvent2026.toml`; it replaces the bundled one entirely, so a copy from an
older version loses newer data such as notes, shuttles and the Wynn/Encore split.) Corrections from people on site are welcome as PRs too.

### Other events

The bundled, hand-checked table is for re:Invent 2026; everything else works for any AWS event
(`--event`). Most events, such as AWS Summits, use one venue and need no table. For a
multi-venue event:

```bash
rip --event some-event sync
rip --event some-event venues measure    # asks first, then saves a measured table
```

It looks up each venue name from the event's catalog, together with the event's city, on
OpenStreetMap, measures the street walks between every pair, and applies the same allowance as
above. Only those public names and the city are sent (to nominatim.openstreetmap.org and
routing.openstreetmap.de), at most one request per second. It shows where each venue was found,
flagging any that's far from the rest, and **asks you to confirm the matches before measuring or
saving anything**. The table goes to `<config dir>/venues/<event>.toml` (delete it to undo).
It won't replace an existing table without `--force`, which says what it's replacing and backs up
your own file first. Found places are saved in the table and reused on reruns (`--regeocode`
looks them up again). It handles up to 12 venues; sessions with no venue listed are reported,
not measured.

## Using it from Claude Code

Claude Code can drive `rip` for you. This repo includes a skill,
[`.claude/skills/reinvent-planner`](.claude/skills/reinvent-planner/SKILL.md), that Claude Code
picks up automatically when you work in a clone. To use it in every project, copy that folder
to `~/.claude/skills/`. Then ask things like:

- "Find 300-level agent workshops on Tuesday that don't clash with my plan."
- "Check my re:Invent plan for overlaps and walks I can't make."
- "Rank AIM301 first with AIM302 as its backup." (it asks first)
- "What would `rip reserve` try, in order?" (a dry run; nothing is reserved)

What the skill teaches Claude:

- **Keep output short:** `--offline` (the cached schedule, no API calls) and `search --limit`.
- **Read warnings correctly:** an *overlap* is a real conflict; a *tight* walk is only a
  warning, because travel never blocks a reservation.
- **Reservations:** run `rip reserve --dry-run` before any real reservation, respect the
  fair-use limits (one run at a time, the pause between runs, no retry loops), and check a
  "may or may not be reserved" result with `rip schedule`.
- **Ask you first, and never add `--yes` on its own,** for anything that:
  - changes your schedule (favorites, personal time, `reserve`, `cancel`);
  - changes your ranking (it's what `rip reserve` reserves);
  - publishes or unpublishes the calendar feed (and says so when personal time or the
    ranking would be included);
  - changes travel-time corrections or measures a new table (which sends venue names to
    OpenStreetMap);
  - signs you out, or replaces the catalog with `sync --force`.
- **Leave sign-in to you:** you run `rip login` / `rip setup` yourself, since they open a
  browser.
- **Treat the feed link as a secret:** it doesn't repeat `rip calendar url` output unless you
  ask for it.
- **Don't launch the app:** `rip tui` and `rip ui` are interactive, so it suggests them to you
  instead.

Your Claude Code permission prompt is the final check: approve a command with `--yes` only for
a change you've confirmed.

## Also useful: the official MCP server

If you use an AI assistant, AWS's MCP server exposes the same API as tools. For Claude Code:

```bash
claude mcp add --transport http --scope user awsevents \
  https://api.awsevents.com/mcp --callback-port 8484 --client-id 7vmom55m1qstvq8i71ph127bfq
```

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

CI also runs the tests against the oldest dependency versions `pyproject.toml` allows
(`uv sync --resolution lowest-direct`), so its lower bounds stay honest.

To re-record the demo GIF after a UI change, install [vhs](https://github.com/charmbracelet/vhs)
and run `vhs scripts/demo/demo.tape`. It uses a made-up week in fresh temp folders
(`scripts/demo/seed_demo.py`, which refuses to touch your real data).

Live checks against the real API are opt-in. They catch API drift: the published OpenAPI spec is
compared with our models, and real responses must parse.

```bash
uv run pytest -m live                     # read-only; signed-in checks use your `rip login`
RIP_LIVE_WRITES=1 uv run pytest -m live   # adds one favorite and one personal-time block, then removes both
```

### Releasing

Bump `version` in `pyproject.toml`, merge to `main`, then push a matching tag (`v0.1.0`). The
release workflow checks the tag is on `main` and matches the version, runs the tests, builds in a
clean job, and publishes to PyPI with Trusted Publishing and signed attestations, so no token is
stored anywhere.

One-time setup. PyPI trusts the workflow file at the tagged commit, so these settings matter:

1. On PyPI, add a trusted publisher: this repository, workflow `release.yml`, environment `pypi`.
2. In GitHub **Settings → Environments → pypi**: add required reviewers, and limit deployments to
   tags matching `v*`.
3. In GitHub **Settings → Rules → Rulesets**: add a tag ruleset for `v*` that only maintainers
   can bypass, so nobody else can create release tags.
4. Protect `main` (require pull request reviews), since "the tag is on main" is what's trusted.

The workflow's own "tag is on main" check catches accidental tags only: it runs from the tagged
commit, so someone able to tag an unmerged commit could remove it there. Steps 2–4 are the real
protection.

Tests use made-up sessions only. **Never commit real catalog data, API responses, or tokens:**
a registered event's catalog isn't public.

## License

[Apache-2.0](LICENSE)
