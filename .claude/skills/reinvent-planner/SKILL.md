---
name: reinvent-planner
description: Use the `rip` (reinvent-planner) CLI to plan an AWS re:Invent schedule — search the session catalog, rank picks with backups, check clashes and walking time between venues, build the reservation checklist, and reserve. Use when the user asks about re:Invent sessions, their re:Invent schedule or plan, what to reserve, venue travel, or reservation day.
---

# Planning re:Invent with `rip`

`rip` (also installed as `reinvent-planner`) works on the official AWS Events API. The
catalog, ranking and travel table are local; the schedule (reservations, favorites, personal
time) lives on the user's real event account. Run it with the Bash tool.

## Keep output small

- Prefer `--offline` on `plan`, `schedule`, `checklist`, `csv`, `ics` and
  `search --favorites/--reserved`: it uses the cached schedule, makes no API calls, and is
  fast. (If the user published a calendar feed with auto-update, these commands without
  `--offline`, and `sync`, also refresh the feed's gist when it changed: expected, but another
  reason to prefer `--offline`.) Drop it only when the user
  wants fresh data (or run `rip sync` / `rip schedule` once to refresh the cache).
- Always pass `--limit` (`-n`) to `search`, e.g. `-n 15`. Narrow with filters rather than
  reading hundreds of rows: `--day tue --level 300 --type workshop --topic agents --venue
  mgm --laptop --available`.
- Set `COLUMNS=160` so tables don't wrap.
- Use `rip show CODE` for one session's full details rather than a wide search.

## Reading the output

- Search marks: `R` reserved, `F` favorite, `#n` the user's rank.
- `plan` / `checklist` flag two different things:
  - **overlap** — two sessions at the same time; only one can happen.
  - **tight** — e.g. "30 min to get from Venetian to MGM Grand, needs ~45": the walk is
    longer than the gap. This is a warning, never a blocker; re:Invent runs on a 30-minute
    grid, so many back-to-back sessions in different venues are tight. Say it plainly and let
    the user decide.
- Distances are walking (OpenStreetMap) plus 10 minutes to reach the room. `rip venues`
  shows the table and monorail tips.

## Changing things: always ask first

These change the user's real event schedule, their sign-in, local settings that drive
reservations, or send data elsewhere. Never add `--yes`/`-y`, and never run the ones without a
prompt, unless the user has explicitly confirmed that exact change in the conversation:

- `rip fav add CODE…` / `rip fav rm CODE`
- `rip time add|edit|rm …` (personal time, entered in the event's time zone)
- `rip reserve` / `rip cancel CODE`
- `rip rank set …` / `rip rank rm …`: the ranking is exactly what `rip reserve` reserves, so
  only change it when asked. It's stored locally, but if the calendar feed was published
  with `--ranked`, rank changes also update the gist.
- `rip calendar publish|unpublish`: a secret GitHub Gist that anyone with the link can read.
  `--personal` / `--ranked` put personal time / the ranking in it; say so before running.
- `rip venues set A B MIN` (no prompt: overwrites a travel-time correction),
  `rip venues reset [A B]`, and `rip venues measure [--force]` (sends venue names and the city
  to OpenStreetMap, then writes a travel table).
- `rip logout [--builder-id]`, `rip sync --force` (replaces the catalog even if it shrank).
- `rip login` and `rip setup` open a browser sign-in: ask the user to run them themselves.

Never run a prompting command without `--yes` just to see its prompt: there's no terminal, so it
hangs. Describe the exact command and its effect, get a clear yes, then run it with `--yes`.

`rip calendar url` prints the secret feed link: treat it as a password and don't repeat it
unless the user asks for it.

Read-only, safe to run any time: `search`, `show`, `plan`, `schedule`, `checklist`,
`rank ls`, `fav ls`, `time ls`, `whoami`, `events`, the bare `rip venues`, and `sync` (an API
read plus a local catalog update). `rip csv` / `rip ics` only write local files (`--out` /
`--out-dir` say where; `csv -o -` prints to the terminal).

Don't run `rip tui` or `rip ui` yourself: they're interactive and long-running (`ui` serves a
local web page). Suggest them to the user for browsing.

## Reservations

- re:Invent 2026: the event website opens reserved seating **Oct 6**; the API opens
  **Oct 8** (before that, API reservations return "not open" and change nothing).
- Before any real reservation, run `rip reserve --dry-run` (it needs sign-in and reads the
  live schedule, but sends no reservations) and show the user what would be tried, in order,
  including backups. `rip reserve CODE…` reserves those codes instead of the ranking. Exit
  codes: 0 all reserved, 2 some picks unfilled, 1 stopped early. Only then, with their go-ahead, `rip reserve --yes`.
- `rip reserve` has fair-use limits built in: one run at a time on the machine, a one-minute
  pause between runs (longer after "not open"), no polling. Don't loop or retry it; if it
  says to wait, tell the user.
- A pick reported as "may or may not be reserved" needs `rip schedule` to confirm before
  anything else is tried.
- For website reservations (Oct 6), `rip checklist --out checklist.md` is the ordered list.

## Ranking workflow

1. `rip search … -n 15` to find candidates, `rip show CODE` to check one.
2. `rip rank set CODE 1 --backup CODE2` (repeat with 2, 3, …). Warnings about clashes and
   tight walks appear right away.
3. `rip plan --offline` to review the day, then `rip checklist --offline`.

## Notes

- Sign-in: `rip whoami`; if signed out, the user runs `rip login` themselves (it opens a
  browser).
- `rip sync` downloads the full catalog (a minute or so): run it when `rip` says the catalog
  isn't downloaded or is stale, not just because a narrow search found nothing.
- Other events: `--event ID` (`rip events` lists them).
