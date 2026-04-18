# Dashboard User Database Automation Proposal

Status: Draft / research complete, no code written
Target component: `dashboard/` (HBlink4 core is explicitly out of scope)
Author context: research performed April 2026

---

## 1. Current State

The dashboard performs callsign lookups from a single static file at `/home/cort/hblink4/user.csv` (7.77 MB, 136,613 rows). The file is obtained by manually downloading a full CSV dump from radioid.net, then running `scripts/filter_user_csv.py` to strip everything outside the United States and Canada. The CSV carries seven columns (RADIO_ID, CALLSIGN, FIRST_NAME, LAST_NAME, CITY, STATE, COUNTRY) but only the first two are consumed: `dashboard/server.py::load_user_database` (lines 31-61) builds a single `Dict[int, str]` of `{radio_id: callsign}` at module import time and throws the rest away. That dict has exactly one reader: the `stream_start` event handler (line 684), which does `user_database.get(src_id, '')` to decorate each call with the operator's callsign. Neither the running dashboard nor hblink4 core refreshes the file; updates only happen when a human re-runs the manual pipeline, which is why the data goes stale and users complain. The hblink4 DMR server itself does not consume `user.csv` at all; the user has explicitly opted out of paying that lookup overhead in the hot path.

---

## 2. radioid.net Data Sources

### 2.1 Static file dumps

- **Catalog page**: `https://radioid.net/database/dumps` (Cloudflare-challenge-protected, browser only).
- **Direct file URLs** (confirmed by community usage — see `HBMonv2`, Pi-Star, and DVSwitch references in section 9):
  - `https://database.radioid.net/static/user.csv` — DMR user CSV
  - `https://database.radioid.net/static/users.json` — same data, JSON array
  - `https://database.radioid.net/static/rptrs.json` — repeater database
- **Update cadence**: daily regeneration ("free daily data dump"). Pi-Star's `DMRIds.dat` pipeline (which pulls from this same source) notes that newly-issued IDs can lag by up to ~24 hours before appearing.
- **Size**: the unfiltered user CSV is considerably larger than the 7.7 MB filtered file on disk. Empirically the full CSV is on the order of 20-30 MB as of 2026-Q2 (303,581 records reported by the API's pagination metadata; each CSV row averages ~70-100 bytes, so ~25 MB is a reasonable planning figure).
- **Conditional GET**: The static files are served by a standard web stack behind Cloudflare; `Last-Modified` is almost certainly present and `ETag` is likely. We could not directly confirm the exact response headers in this research pass because `WebFetch` hit the 10 MB size limit trying to pull the full CSV, and Cloudflare's challenge blocked HEAD-style inspection through the tool. **This should be verified at implementation time** by running a manual `curl -I https://database.radioid.net/static/user.csv` from a real client with a descriptive User-Agent. If neither header is available we fall back to storing our own file-size / content-hash fingerprint to suppress redundant writes.
- **Compression**: no documented `.gz` variant. HTTP-level gzip via `Accept-Encoding: gzip` is standard on their web stack and should be requested explicitly.
- **Authentication**: none for the static files. They are anonymous HTTPS downloads.

### 2.2 JSON API

- **Base URL**: `https://radioid.net/api/` (or the functionally equivalent `https://database.radioid.net/api/`).
- **User lookup endpoints**:
  - `/api/users` — paginated list of all DMR users
  - `/api/dmr/user/` — single / filtered DMR user lookup
  - Analogous Cap+ / NXDN endpoints exist but are irrelevant to this project.
- **Query parameters on `/api/dmr/user/`**:
  - `id` (and `id_sel` for match-mode: exact / begins-with)
  - `callsign`, `callsign_sel` (exact / left / begins / ends)
  - `name`, `city`, `state`, `country` (with matching `_sel` suffixes)
  - `%` wildcard supported, e.g. `callsign=VE9%`
  - Multi-select via repeated keys, e.g. `country=Canada&country=United+States`
  - `page` (1-indexed) and `per_page` (max **200**)
- **Response shape** (example pulled during research):
  ```json
  {
    "page": 1,
    "pages": 1518,
    "count": 303581,
    "results": [
      {
        "id": 1023007,
        "radio_id": 1023007,
        "callsign": "VA3BOC",
        "fname": "Hans Juergen",
        "name": "Hans Juergen",
        "surname": "",
        "city": "Cornwall",
        "state": "Ontario",
        "country": "Canada",
        "has_valid_callsign": "1",
        "has_valid_email": 0,
        "lastheard": "Thu, 16 Apr 2026 18:07:11 GMT",
        "lastmaster": "2341",
        "lastsource": "2341671",
        "lasttg": "91"
      }
    ]
  }
  ```
- **Authentication**: optional `X-API-Token` header. None required today for read access.
- **Rate limits / terms** (see section 2.3): not numerically published; enforcement is at the server's discretion.
- **Bandwidth cost of a full walk**: 303,581 records at `per_page=200` = **1,518 round trips**. That is roughly two orders of magnitude more requests than a single static-file download. Doing this daily, per dashboard instance, is exactly the kind of bulk-mirroring behavior the terms warn against.

### 2.3 Terms of service summary

The Cloudflare challenge page prevented direct scraping of `radioid.net/terms_and_conditions_policy`, but the same text is surfaced in web search excerpts and is corroborated by multiple community discussions. The operative rules for our use case are:

1. **User-Agent identification is required for automated clients.** It must include the application name and a contact address.
2. **Normal lookups are allowed.** Bulk mirroring, scraping, re-publishing as a public feed, operating a competing directory, or commercial use all require **prior written permission**.
3. The service explicitly reserves the right to **rate-limit, require authentication, revoke access, or block abusive clients**. No numeric RPM/RPS is published; enforcement is reactive.
4. **Daily updates are the intended cadence** for redistributors — Pi-Star, the DMR radio contact-list ecosystem, and HBMonv2 all sync at that interval.

The user's instinct that "daily is the maximum" matches the publisher's clear preference. Hourly or sub-daily polling of the static file would be wasteful (the data only regenerates once per day), and walking the API in full is out of bounds per the terms.

### 2.4 Side-by-side comparison

| Dimension | Static file | JSON API |
| --- | --- | --- |
| URL | `https://database.radioid.net/static/user.csv` | `https://radioid.net/api/dmr/user/?page=N&per_page=200` |
| Request count for full refresh | 1 | ~1,518 |
| Bytes transferred (refresh) | ~25 MB raw, ~5-8 MB gzipped | similar total, spread over 1,518 responses |
| Fields per record | 7 | 13-15 |
| Fields we actually need | 2 (RADIO_ID, CALLSIGN); 3 with COUNTRY for filtering | 2; 3 with country |
| Auth | none | optional token |
| Rate-limit exposure | minimal — single GET | high — 1,518 GETs with risk of ban |
| Conditional GET | Last-Modified (likely) | none on paginated endpoint |
| Update cadence upstream | daily | continuous (but we don't need that) |
| Terms posture | "normal lookup" — our use is a single daily GET, well inside acceptable | "bulk mirroring" — explicitly discouraged without written permission |
| Filtering at server | none (full dump) | yes (`country=...`) |

---

## 3. Recommended Approach

### 3.1 Data source: static file, not the API

Pull `https://database.radioid.net/static/user.csv` once per day. Rationale:

- We need `radio_id -> callsign` for 136k North American operators. That is a full directory slice, not a targeted lookup. Walking 1,518 paginated API pages to reconstruct the same table is the textbook case of what radioid.net's terms tell us not to do.
- A single conditional GET with `If-Modified-Since` is the lightest-weight request possible and will almost always return `304 Not Modified` between their daily regenerations.
- Every major downstream in the ecosystem (Pi-Star, HBMonv2, the DMR contact-list generators) uses the same static file. Mirroring that convention keeps us on the well-trodden path and makes any future outreach to radioid.net straightforward ("we behave like Pi-Star").

The full CSV carries five columns beyond what we consume, which is mildly wasteful of bandwidth — but filtering at download time to a compact in-memory dict solves the on-disk cost (section 3.2) and the daily ~5-8 MB gzipped transfer is negligible.

### 3.2 Storage format: filtered JSON snapshot

**Filter at download time, store as a compact JSON dict file.** The dashboard loads it at startup and on reload. Concretely:

- The download/refresh pipeline pulls the full CSV, streams through it row by row, applies the country/callsign/ID filter, and emits a dict `{"<radio_id>": "<callsign>", ...}` serialized as JSON.
- Target file: `dashboard/data/user_db.json`.
- Expected on-disk size: 136k entries × ~22 bytes per `"1234567": "WX1YZ",` = ~3 MB. That is **less than half the current 7.7 MB CSV on disk**, while carrying the same information the dashboard actually uses.
- Load time at server startup: a single `json.load` into a pre-sized dict is faster than the current `csv.DictReader` loop and skips the per-row try/except.
- Update atomicity: same pattern already used for `stats.json` and `last_heard.json` in the dashboard — write to `user_db.json.tmp` and `os.replace()` it into place. The dashboard already has orphan `.tmp` cleanup logic (`_cleanup_temp_files` in `DashboardState`) that this integrates with cleanly.

**Alternatives considered:**

- **Keep CSV.** Pro: no format change, `filter_user_csv.py` is already written. Con: the dashboard wastes memory/parse time on 5 columns it never reads, and on-disk size is 2-3x larger than needed. Rejected.
- **SQLite.** Pro: lazy lookup, no full-dict resident in memory. Con: the dashboard already has the full dict in RAM and it's only a few MB; lookup is per-stream-start (rare-ish) so DB overhead per call is harder to justify than a hash lookup. The complexity is not warranted. Rejected.
- **Pickle.** Pro: fastest load. Con: Python-version coupling, opaque on disk, footgun for ops. Rejected.
- **msgpack.** Pro: slightly smaller than JSON. Con: adds a dependency for marginal savings (maybe 0.5 MB). Rejected.

JSON wins on operator-friendliness (grep-able, diff-able, inspectable with `jq`) at negligible cost versus the alternatives.

**Keep the CSV as a transient artifact**, not a permanent one: the downloaded raw CSV goes to a temp path, gets filtered into `user_db.json`, then the CSV is deleted. We never carry the full unfiltered file on disk.

### 3.3 Filter scheme

Extend `dashboard/config.json` with a `user_database` block. Example:

```json
"user_database": {
    "enabled": true,
    "source_url": "https://database.radioid.net/static/user.csv",
    "local_path": "data/user_db.json",
    "user_agent": "HBlink4-Dashboard/1.x (+https://github.com/<project>; contact=operator@example.org)",
    "refresh": {
        "schedule": "daily",
        "time_of_day": "03:17",
        "jitter_minutes": 15,
        "on_startup_if_older_than_hours": 36
    },
    "filter": {
        "countries": ["United States", "Canada"],
        "callsign_regex": null,
        "radio_id_ranges": null
    },
    "fallback": {
        "keep_stale_on_failure": true,
        "min_rows_required": 1000
    }
}
```

**Filter semantics** (all optional; all applied as an AND):

- `countries`: list of exact strings matched against the `COUNTRY` column. Special value `"all"` (string, not list) disables the country filter entirely. Default `["United States", "Canada"]` preserves current behavior.
- `callsign_regex`: optional Python regex applied to the `CALLSIGN` column after strip. `null` disables. Useful for operators who want, say, only US/Canada amateur calls (`^[KNWAV][A-Z]?\d[A-Z]{1,3}$`).
- `radio_id_ranges`: optional list of `[low, high]` inclusive integer pairs. `null` disables. Useful for testing or for operators who want to scope to specific DMR-MARC blocks.

The country list matches what `scripts/filter_user_csv.py` does today verbatim, so default config reproduces current behavior byte-for-byte.

### 3.4 Update schedule

Config-driven, with a **daily-or-less-often floor** enforced in code:

- `refresh.schedule`: one of `"daily"`, `"weekly"`, or `"disabled"`. (We intentionally do not expose `"hourly"` — the upstream only regenerates daily, and sub-daily polling is purely wasteful. If a user writes `"hourly"` in the config, the loader logs a warning and clamps to `"daily"`.)
- `refresh.time_of_day`: 24h `HH:MM` in the dashboard's local timezone. Default `"03:17"` — deliberately an odd-minute off-hour to avoid the top-of-hour stampede that hits radioid.net from thousands of Pi-Star installs every night.
- `refresh.jitter_minutes`: integer 0-60. The scheduler picks a random offset in `[0, jitter_minutes)` each day, so repeated dashboard restarts don't settle on the exact same wall-clock second. Default 15.
- `refresh.on_startup_if_older_than_hours`: if the local `user_db.json` is missing or older than this threshold, do one catch-up refresh at startup. Default 36 hours. This handles the "server was off for a long weekend" case without firing on every restart.

A cron-style expression is **not** proposed. The scheduling needs are trivial ("once a day, at about this time") and a full cron parser is overkill; `time_of_day + jitter` covers the realistic cases and is easier for operators to reason about.

### 3.5 Failure / fallback behavior

Failure modes to handle:

1. **radioid.net unreachable / 5xx / timeout.** Keep the existing `user_db.json`. Log a warning. Schedule a single retry with exponential backoff (5 min, 15 min, 60 min), then give up until the next scheduled refresh. Never remove the old file.
2. **HTTP 304 Not Modified** (expected path on most days). Touch the local file's mtime so the "older than X hours" startup check stays fresh, log at DEBUG, do nothing else.
3. **HTTP 4xx** (e.g., 403 because we got rate-limited or UA-banned). Log an ERROR including our User-Agent string so the operator can see what we identified as. Back off aggressively — do not retry for 24h. Keep the existing file.
4. **Malformed CSV** (truncated transfer, wrong encoding, header row missing).  Download goes to a temp path; parse from temp; if parse raises or yields zero valid rows, delete the temp file and keep the old `user_db.json`. The full-dump CSV has a stable column ordering and header — we validate that `RADIO_ID`, `CALLSIGN`, and `COUNTRY` columns all exist before accepting the download.
5. **Filter produces fewer than `fallback.min_rows_required` rows.** Treat as a malformed/suspicious download. Delete the temp artifact, keep the old file, log WARNING. Default floor is 1,000 — a sanity check that catches the case where the upstream serves an error page with a `.csv` Content-Type, or where a future column rename silently drops all rows.
6. **Zero-byte or gzipped-as-empty response.** Same as malformed — fail closed on the update, fail open on serving (old data wins).

The guiding principle: **the dashboard always keeps serving the last known-good user database.** There is no code path where a failed refresh makes callsign lookups worse than they were before. The only "fail closed" behavior is on the write side — we refuse to replace a good file with a bad one.

### 3.6 Concurrency / atomic swap

The dashboard is a single-process asyncio app. The concurrency model is cooperative, so the swap can be very simple:

- `user_database` is currently a module-level `Dict[int, str]` built at import time (line 61). Promote it to a mutable reference held by `DashboardState` (or a small dedicated `UserDatabase` wrapper) so the refresh task can replace it wholesale.
- The refresh task runs as an `asyncio` background coroutine registered alongside `midnight_reset_task()` in `startup_event()`. It does its download + filter + disk-write off the event loop using `asyncio.to_thread` (or `loop.run_in_executor`) so a slow HTTP fetch doesn't block stream_start handling.
- Once the new filtered dict is fully built in memory, swap the reference atomically: Python's dict assignment is a single bytecode op under the GIL and the read path is a single `.get()` call, so readers either see the old dict or the new dict — never a partial state. No lock needed.
- The backing file swap uses the existing `tmp -> os.replace()` pattern. Readers of the file (there's only one — startup) are not concurrent with writers (background task running while the app is up), so the file-level atomicity is mostly for crash safety, not reader/writer contention.
- A fresh snapshot is written to disk on successful filter. Subsequent dashboard restarts pick up that snapshot without touching the network unless `on_startup_if_older_than_hours` is exceeded.

The existing websocket clients do not need to be notified when the user database reloads — callsigns are only injected at `stream_start`, so the next transmission after a refresh automatically benefits, with zero client-visible glitch.

---

## 4. Fate of `scripts/filter_user_csv.py`

**Deprecate, but keep it around as a documented manual fallback.** Rationale:

- Its logic (country whitelist → CSV row filter → atomic replace) is a strict subset of what the new in-dashboard pipeline will do. Duplicating it in two places is a maintenance hazard.
- However, operators who prefer the current manual workflow — or who run the dashboard in air-gapped environments and shuttle files in by hand — need a path that doesn't depend on the dashboard being able to talk to radioid.net.
- Proposed plan: add a deprecation notice at the top of the script pointing at the new automated refresh, but leave it functional. Over a release or two, once the automated path has been exercised in production, decide whether to remove it outright or fold it into a `python -m dashboard.refresh_user_db` entry point that does both the download and the filter from the command line for manual invocation.

The automated dashboard pipeline should expose its filter logic as a small importable function (e.g., `dashboard.user_db.filter_rows(reader, config) -> dict`) so the filter-only case can be driven from a CLI wrapper without reimplementing anything.

---

## 5. Implementation Sketch

**Config schema addition.** Add the `user_database` block to `dashboard/config.json` and `dashboard/config_sample.json` exactly as shown in section 3.3. The existing `load_config()` in `server.py` already merges user config over a `default_config` dict, so setting sane defaults there preserves backwards compatibility for anyone who doesn't add the block. No schema version bump needed.

**New module: `dashboard/user_db.py`.** Owns the user database lifecycle. Exposes `UserDatabase` class with: `.get(radio_id) -> str`, `.reload_from_disk()`, and `.refresh_from_upstream(config)`. Internally holds the `Dict[int, str]`, the path to the on-disk snapshot, and a small metadata sidecar (last-modified header received, row count, refresh timestamp). Replaces the current top-level `user_database` module global in `server.py`; the `stream_start` handler changes from `user_database.get(src_id, '')` to `state.user_db.get(src_id, '')`.

**Background scheduler.** A new coroutine `user_db_refresh_task()` registered next to `midnight_reset_task()` in `startup_event()`. Computes the next refresh time as `today @ time_of_day + random.uniform(0, jitter_minutes) * 60s`, sleeps until then, runs the refresh, repeats. Also runs one catch-up refresh on startup if the snapshot is older than `on_startup_if_older_than_hours`. All network and parse work goes through `asyncio.to_thread` to keep the event loop responsive.

**Download/filter/store pipeline.** Three stages, each independently testable:

1. *Download*: HTTPS GET with `User-Agent` header from config, `If-Modified-Since` header populated from the sidecar metadata, `Accept-Encoding: gzip`. Honor 304 as a no-op. Write response body to `data/user_db.csv.tmp`. Enforce a reasonable timeout (30s connect, 120s total) and a max size (say, 100 MB) to defend against runaway responses. Log the `User-Agent` at startup so operators can grep for their own string in radioid.net's eyes.
2. *Filter*: stream the temp CSV through `csv.DictReader`, apply the configured country / regex / ID-range filters, build `{int(radio_id): callsign.strip()}`. Sanity-check against `fallback.min_rows_required`. Emit a Python dict in memory.
3. *Store*: serialize dict as JSON to `data/user_db.json.tmp`, `os.replace()` into place, delete the CSV temp artifact, update the sidecar, swap the in-memory reference in `UserDatabase`.

**Reload mechanism.** Just the reference swap described in section 3.6. No websocket broadcast, no cache invalidation elsewhere in the app — the dashboard has exactly one reader and it's always read-through.

**Logging.** Every refresh (success, 304, failure, skipped-due-to-malformed) emits a single INFO-level log line with: trigger (startup vs scheduled), HTTP status, bytes transferred, row count before and after filter, duration. Errors log ERROR. This slots into the existing journald output from `hblink4-dash.service` with no systemd changes.

**systemd / deployment.** No changes required to `hblink4-dash.service`. The dashboard's virtualenv already has `requests` or can have it added (the dashboard currently uses `fastapi`/`uvicorn`; a lightweight `httpx` or `urllib.request` call for a once-a-day fetch needs no new heavy dependency — plain `urllib.request` with gzip decoding is sufficient and already in the stdlib).

---

## 6. Open Questions / Tradeoffs

1. **Should hblink4 core eventually consume this too?** The user has said no for now (lookup overhead on the hot path). But once the dashboard has a clean, filtered, small `user_db.json` on disk, it would be trivial for core to memory-map it or load it lazily. Decision needed: leave this door closed, or mention the snapshot file in the core config docs as "available for optional use by integrators"?

2. **Manual-refresh button in the dashboard UI?** It's a ~30-line addition: a `POST /api/user_db/refresh` endpoint that triggers the same coroutine the scheduler calls, guarded by a rate-limit (one refresh per 5 minutes regardless of source). Useful for ops, but also a potential abuse surface if the dashboard is public-facing. Default off, opt-in via config?

3. **Expose refresh status via WebSocket?** The existing initial-state payload could carry `user_db: {row_count, last_refresh, source: "radioid.net"}` so the UI can show a small "DB updated 4h ago" indicator. Low value, low cost. Decision needed: include in v1 or defer?

4. **Alerting on persistent failure.** If 72h go by without a successful refresh (e.g., radioid.net is down, or they banned our User-Agent), should the dashboard surface that somewhere the operator will notice — journald ERROR is easy to miss. Options: (a) append to `state.events` so it shows in the event feed; (b) a dedicated status banner; (c) do nothing, trust the operator to monitor logs. Low urgency given the fallback behavior (old data keeps working), but worth a decision.

5. **User-Agent contents.** The terms require an identifying UA with contact info. The sample config uses `contact=operator@example.org` as a placeholder. Decision needed: require operators to override this (refuse to start / log a warning if it's still the placeholder)? Or leave as best-effort with a warning only?

6. **Should `scripts/filter_user_csv.py` be promoted to a `python -m dashboard.refresh_user_db` CLI entry point** immediately as part of this work, or left as-is with a deprecation notice and revisited later? The former is cleaner but expands the scope; the latter keeps this change surgical.

7. **Compression at rest?** `user_db.json` at ~3 MB is not a burden today. If the filter is widened to "all countries" (~300k records, ~8-10 MB JSON), compressing the on-disk snapshot with gzip is a one-line change that cuts disk footprint 3-4x at the cost of a trivial decompress on load. Worth doing preemptively, or wait until someone asks?

8. **IPv6 / IP pinning for outbound.** radioid.net sits behind Cloudflare; the IP changes. No action needed today, but worth noting: if the deployment is in a tightly firewalled environment, the operator will need to allow outbound HTTPS to Cloudflare-fronted hosts. Mention in the dashboard docs.

---

## 9. Sources

- [RadioID Database Dumps (https://radioid.net/database/dumps)](https://radioid.net/database/dumps)
- [RadioID Database API documentation (https://database.radioid.net/api/)](https://database.radioid.net/api/)
- [RadioID single-user DMR endpoint (https://radioid.net/api/dmr/user/)](https://radioid.net/api/dmr/user/)
- [RadioID Terms and Conditions (https://radioid.net/terms_and_conditions_policy)](https://radioid.net/terms_and_conditions_policy)
- [HBMonv2 config referencing `database.radioid.net/static/user.csv` and 7-day `FILE_RELOAD`](https://github.com/sp2ong/HBMonv2)
- [HBmonitor sample config with RadioID URLs](https://github.com/sp2ong/HBmonitor/blob/master/config_SAMPLE.py)
- [Pi-Star forum discussions on DMRIds.dat update cadence and RadioID URL changes](https://forum.pistar.uk/viewtopic.php?t=3214)
- [DVSwitch discussion of the radioid.net URL change](https://dvswitch.groups.io/g/main/topic/radioid_net_url_update_in/74672336)
- [KV4S DMR UserDB Radio Converter (community tool using the same static dump)](https://github.com/Russell-KV4S/DMR.UserDB.RadioConverter)
