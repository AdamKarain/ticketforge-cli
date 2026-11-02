# TicketForge CLI

A command line client for TicketForge. The API has no public documentation; everything here was reverse-engineered by inspecting the web app's network traffic (Safari Web Inspector + HAR export).

## Install

```
python3 -m venv venv && source venv/bin/activate
pip install requests
```

(Only dependency is `requests`. Tests additionally run with the standard library's `unittest`.)

## Setup

```
python ticketforge.py setup
```

Prompts for your TicketForge username and password (the same account as the web app; register there first). Credentials are stored in `~/.ticketforge.json` with owner-only file permissions, and sent as HTTP Basic auth, which is what the web app itself uses.

## Usage

```
# create
python ticketforge.py create --title "Fix login bug" --description "500 on bad password"

# create with dependencies
python ticketforge.py create --title "Deploy fix" --depends-on TF-695,TF-696

# list
python ticketforge.py list
python ticketforge.py list --limit 10
python ticketforge.py list --limit 5 --all     # follow pagination cursors to the end

# full view of one ticket (dependencies, owner)
python ticketforge.py show TF-695

# update only what you pass; everything else is preserved
python ticketforge.py update TF-695 --stage open --title "New title"
python ticketforge.py update TF-696 --depends-on ""     # clears dependencies

# custom fields
python ticketforge.py fields list
python ticketforge.py fields add --name priority --label Priority
```

## Tests

```
python -m unittest test_ticketforge -v
```

Eight tests cover: create request shape, list + pagination parsing, cursor-following in `--all` (including the guard against an ignored cursor), update's read-modify-write merge, 429 retry behavior, 401 error clarity, and dependency parsing. The API is mocked; tests make no network calls.

Screenshots of a live run against the real API are in `evidence/`.

## The reverse-engineered API (as discovered)

| Method | Path | Notes |
|---|---|---|
| POST | /api/tforge/workitem/publish | create; body: title, description, dependsOn |
| GET | /api/tforge/workitems/mine?limit=N | list; returns workitems + pagination {limit, hasMore} |
| GET | /api/tforge/workitem/REF?view=deep | one ticket incl. dependsOn and owner |
| PUT | /api/tforge/workitem/REF | update; expects the FULL object (title, description, stage, dependsOn, customFields) |
| GET / POST | /api/tforge/custom-fields | list / add (name, label) |

All endpoints: HTTP Basic auth, JSON responses.

## Design notes

- **Update is read-modify-write.** The API's PUT replaces the whole workitem, so the CLI first GETs the current ticket, merges only the flags you passed, and PUTs the full object back. Fields you don't pass are preserved.
- **Rate limiting is handled, not ignored.** The API allows 50 requests per window and advertises state in `x-ratelimit-*` headers. On a 429 the CLI waits until the server's advertised reset time (falling back to exponential backoff) and retries up to 3 times. When fewer than 10 requests remain, the CLI prints a warning to stderr.
- **Errors are human.** 401 says to re-run setup; connection errors name the base URL; other API errors show status + server detail. No stack traces for expected failures.
- **Color when it helps, plain when it doesn't.** Table headers, refs, and stages are colored only when stdout is a terminal (and `NO_COLOR` is respected), so piping the output into other tools yields clean text.

## Assumptions and limitations

- Stage values: the API returned `"open"` for new tickets; the update command accepts any stage string and lets the server validate, since the full enum isn't documented anywhere observable.
- Pagination: the web app's own traffic never paginates, so I probed the API directly: `offset` and `page` parameters are ignored, but responses carry a `nextCursor`, which the API accepts as a `cursor` query parameter. `list --all` follows cursors until `hasMore` is false, with a guard that stops if a cursor is ever ignored (preventing an infinite loop).
- Custom field type: only `text` fields were observable in the web app, so `fields add` creates text fields.
- Deleting tickets: no delete endpoint was observed in the web app's traffic, so the CLI doesn't offer one.

## AI disclosure

I used AI tools (Claude) throughout as a pair programmer: analyzing the HAR capture of the web app's network traffic, drafting the CLI structure, and writing tests. The reverse engineering of the API was done by me in the browser's network inspector; design decisions (read-modify-write updates, rate-limit handling via the server's reset header, config storage) were made and verified by me, and I tested all commands against the live API.
