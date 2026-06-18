# HLTV CS Esports Scraper

A tool for building a **comprehensive, queryable dataset of professional
CS:GO / CS2 statistics** from [HLTV.org](https://www.hltv.org), covering tier
1–3 teams (approximated as the **top ~100 ranked teams** at any point in time)
from CS:GO's 2012 release onward.

It captures as much per-player statistical detail as HLTV exposes — rating,
fragging, KAST/ADR/impact, **opening duels (entries)**, **multi-kill rounds**,
**clutches**, **utility** (flashes / utility damage), and per-map scoreboards —
and stores it **time-sliced into periods** so you can build a player's profile
for a specific era. On top of the raw store it ships an analysis layer for
**modelling a player's contribution to their team** and for studying the
**weapon / map / fragging metas** of each time period.

---

## Why it's built this way

HLTV sits behind Cloudflare and is actively hostile to scrapers. The design
reflects that reality and keeps the fragile parts isolated:

| Layer | Module | Responsibility |
|-------|--------|----------------|
| Config | `config.py` | Rate limits, paths, periods, proxy, backend — all env-overridable (`HLTV_*`). |
| Fetch | `http_client.py` + `browser.py` | Cloudflare-aware backends (**real-browser** via `undetected-chromedriver` → `curl_cffi` → `cloudscraper` → `requests`), one warmed reused session, randomised rate limiting + periodic long breaks, exponential backoff, **disk cache of every page** (so re-parsing never re-hits the network). |
| Parse | `parsers/` | **Pure** `HTML → dataclass` functions. No network, no DB — unit-tested against saved fixtures. This is the only layer that breaks when HLTV changes its markup. |
| Model | `models.py` | Typed dataclasses for every entity. |
| Store | `db.py` | Single-file **SQLite** with idempotent upserts and a scrape log for **resumable** runs. |
| Orchestrate | `scrapers/` | Control flow: which URLs, in what order, across which periods. Resilient — one bad page never aborts a run. |
| Analyse | `analysis/` | Player-contribution model + meta trends, reading straight from SQLite. |

The clean split between **pure parsers** and **effectful scrapers** means the
brittle HTML logic is fully testable offline, and the politeness/anti-bot
behaviour lives in exactly one place.

---

## Install

```bash
pip install -e ".[fetch,analysis]"      # editable install with all extras
# or just the deps:
pip install -r requirements.txt
```

## Quick start

```bash
# 1. Create the database (data/hltv.sqlite3)
hltv-scraper init-db        # or: python -m hltv_scraper.cli init-db

# 2. Build ranking + roster history (defines who is "tier 1-3" over time)
hltv-scraper rankings --start 2013-01-01 --step-days 28

# 3. Scrape per-player, per-period statistics for every discovered player
hltv-scraper players --period-months 6

# 4. Scrape per-map scoreboards (+ derived head-to-head)
hltv-scraper matches --max-pages 50

# 5. Analyse
hltv-scraper analyze contribution --min-maps 20 -o contributions.csv
hltv-scraper analyze fragging-meta -o meta.json
hltv-scraper analyze timeline --player-id 7998        # a player's career arc
hltv-scraper analyze head-to-head --player-id 7998    # biggest duel rivalries

# Assemble a full player profile for a period (identity + stats + contribution
# timeline + head-to-head + role signals) as JSON
hltv-scraper profile --player-id 7998 --start 2018-01-01 --end 2019-12-31 \
    -o s1mple_2018-19.json

# Dump any table to CSV for your own modelling
hltv-scraper export player_stat_periods stats.csv

# One-shot end-to-end
hltv-scraper full --start 2013-01-01
```

Every command is **resumable**: re-running skips pages already completed (per
the `scrape_log` table) and reuses the on-disk HTML cache.

### Getting past Cloudflare (and not getting banned)

HLTV is behind a Cloudflare **JavaScript challenge** and blocks datacenter IPs
(you'll see `FetchError ... HTTP 403`). There are two fetch strategies:

| Backend | How | When it works |
|---------|-----|---------------|
| `curl_cffi` / `cloudscraper` (HTTP) | Browser-TLS impersonation | Basic challenges, from a clean/residential IP. Lightweight. |
| **`browser`** (recommended) | Drives a **real Chrome** via `undetected-chromedriver` (Selenium fallback) | The JS/managed challenge. Heaviest but most reliable. |

```bash
pip install -e ".[browser]"        # needs a local Chrome/Chromium
export HLTV_BACKEND=browser
hltv-scraper rankings              # a real browser window opens and solves CF
```

**Why this avoids bans.** The browser backend launches **one** browser, solves
the challenge **once** during warm-up, and **reuses that warmed session** (its
`cf_clearance` cookie) for every page — just like a person browsing. Combined
with:

* slow, randomised per-request delays (`HLTV_MIN_DELAY`/`HLTV_MAX_DELAY`, default
  **8–16s**);
* a **periodic long break** every N requests (`HLTV_REQUESTS_PER_BREAK`=40,
  `HLTV_BREAK_SECONDS`=90) so a multi-day run looks intermittent;
* **non-headless** by default (Cloudflare fingerprints headless Chrome);
* full **resumability** (stop/resume any time; nothing is re-fetched),

…you can crawl the full history slowly and safely. For extra safety use a
**residential proxy**: `export HLTV_PROXY="http://user:pass@host:port"`.

> Headless server? Run under a virtual display: `xvfb-run -a hltv-scraper ...`,
> or set `HLTV_BROWSER_HEADLESS=true` (less evasive).

**Tuning knobs** (all env vars): `HLTV_BACKEND`
(`browser`/`auto`/`curl_cffi`/`cloudscraper`/`requests`), `HLTV_MIN_DELAY`,
`HLTV_MAX_DELAY`, `HLTV_REQUESTS_PER_BREAK`, `HLTV_BREAK_SECONDS`,
`HLTV_MAX_RETRIES`, `HLTV_PROXY`, `HLTV_BROWSER_HEADLESS`, `HLTV_BROWSER_DRIVER`
(`auto`/`undetected`/`selenium`), `HLTV_BROWSER_WAIT`, `HLTV_BROWSER_BINARY`,
`HLTV_RANKING_FILTER`, `HLTV_CACHE_TTL_DAYS`.

### Recipe: every top team's players, 2012 → today

The weekly **world ranking** only lists ~top 30 and starts in **Sept 2015**, so
for broad ("top ~100", tier 1–3) coverage going back to CS:GO's release we
discover players from HLTV's **stats players index** (`/stats/players`), filtered
to top teams and paged **per period** — this is the `--discover stats` default.

```bash
export HLTV_BACKEND=browser            # real browser, warmed session
export HLTV_RANKING_FILTER=Top50       # HLTV's broadest team filter (~tier 1-3)

hltv-scraper init-db
hltv-scraper rankings --start 2015-09-01            # roster history (2015+)
hltv-scraper players  --start 2012-08-21 \
    --period-months 6 --min-map-count 20            # per-player, per-6-months
hltv-scraper matches  --start 2012-08-21 --max-pages 200   # scoreboards + H2H

# then build profiles / analyse
hltv-scraper profile --player-id 7998 --start 2018-01-01 --end 2019-12-31
```

This is intentionally a **long, slow** crawl (days, not minutes) — that's the
point. It's fully resumable, so run it in chunks. `rankingFilter` caps at
`Top50` on HLTV; for the widest net set `HLTV_RANKING_FILTER=ALL` and lean on
`--min-map-count` to keep it to genuine pros.

---

## What gets stored

SQLite tables (see `db.py` for the full schema):

* **teams**, **players** — identity.
* **team_rankings** — weekly world-ranking snapshots (rank + points).
* **roster_memberships** — who was on each team at each snapshot (roster history).
* **player_stat_periods** — the heart of it: one row per *(player, period,
  ranking-filter)* with ~45 metrics — rating, KPR/DPR/APR, KAST, impact, ADR,
  total kills/deaths, HS%, **opening kills/deaths/success/rating**, **multi-kill
  rounds (0–5k)**, **clutches (1v1–1v5)**, and **utility** (flash assists,
  util damage/round, grenade dmg, saves).
* **map_stats** + **player_map_performance** — per-map scoreboards (K/A/D, +/-,
  ADR, KAST, rating per player per map): the granular source.
* **head_to_head** — directed player-vs-player kill tallies per context.
* **weapon_kills** — per-player weapon usage for weapon-meta analysis.
* **events** — tier / prize-pool / dates for weighting by competition calibre.
* **scrape_log** — bookkeeping for resumable scrapes.

---

## The contribution model

`analysis/contribution.py` turns the component stats into an **explainable**
contribution score, decomposed into facets — *fragging, entry, clutch, utility,
trade/survival, consistency* — each normalised against rough tier-1 anchors.
Unlike HLTV's black-box Rating 2.x, every weight is a documented, tunable
constant:

```python
from hltv_scraper.db import Database
from hltv_scraper.analysis.contribution import compute_contributions, DEFAULT_WEIGHTS

db = Database("data/hltv.sqlite3")
for bd in sorted(compute_contributions(db, min_maps=20),
                 key=lambda b: b.score, reverse=True)[:10]:
    print(bd.player_id, round(bd.score, 3), bd.facets)
```

### Player profiles

`analysis/profile.py` (CLI: `profile`) stitches the store into one object per
player and period — identity, team history, the per-period stat records, the
contribution timeline, top **head-to-head** rivalries (kills for/against and net
diff), and derived **role signals** (entry / impact / utility / consistency
indices). This is the concrete "build a profile for a player during a period"
deliverable.

Because the **per-period component stats are persisted**, the natural next step
is to *learn* the weights: regress these facets against round-/match-win
outcomes (from `player_map_performance` + `map_stats`) to replace the default
prior with a fitted model. The plumbing for that is all here.

## Meta analysis

`analysis/meta.py` aggregates along the time axis so contribution can be
**era-adjusted**:

* `weapon_meta(db)` — kill share per weapon per period.
* `fragging_meta(db)` — league-wide average rating components per period (the
  baseline to normalise against, e.g. ADR inflation across CS2).
* `map_meta(db)` — map-pool popularity by year.

---

## Tests

Pure parsers and the full pipeline are tested offline against fixtures in
`tests/fixtures/` (no network needed):

```bash
python -m unittest discover -s tests
```

## Notes & limitations

* **Selector drift**: HLTV periodically restyles its pages. Parsers match
  defensively (fuzzy stat labels, multiple fallback selectors), but when a page
  changes, fixes are confined to `parsers/`. Add a saved page to
  `tests/fixtures/` and the change is covered by a test.
* **Head-to-head** is derived from map scoreboards (kills attributed across the
  opposing five) since HLTV's true per-duel endpoint is sparse; rows are scoped
  by map id so you can re-aggregate however you like.
* **Be polite / respect HLTV's ToS.** Defaults are intentionally slow. This is
  for research/educational analysis of public statistics.
