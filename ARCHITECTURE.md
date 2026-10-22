# reel-scout — Architecture & Build Plan

> Web service: paste a link (Instagram post/reel, Telegram post, TikTok, arbitrary
> website) → extract text/audio/images → find movie titles → approve poster matches →
> push to a **PunchPlay** list → one-click **Radarr** import and **Kometa** collection.
>
> Codename `reel-scout` (folder name adjustable). Modeled on the existing
> `containers/instragram-recipe-extractor` (same yt-dlp + audio→text + LLM pattern)
> and `containers/trakt-to-letterboxd` (device-code OAuth pattern).

---

## 1. User flow

```
┌────────────┐   ┌─────────────┐   ┌───────────────┐   ┌────────────┐   ┌──────────────┐
│ 1. Paste   │──▶│ 2. Ingest & │──▶│ 3. Find movie │──▶│ 4. Approve │──▶│ 5. PunchPlay │
│    link    │   │    to text  │   │    titles     │   │   posters  │   │  + actions   │
└────────────┘   └─────────────┘   └───────────────┘   └────────────┘   └──────────────┘
                                                                          │  ├─ Add to Radarr
                                                                          │  └─ Create Kometa
                                                                          └─────  collection
```

1. **Paste link** — single input box; optional list name.
2. **Ingest** — download media + text via `yt-dlp` (video/reel), page scrape (website),
   Telegram/Instagram caption. Extract audio → transcribe. OCR/vision on images (optional).
3. **Extract titles** — LLM (copilot-api) returns a structured JSON list of candidate
   movie titles (+ year hints).
4. **Match & approve** — each title resolved against **TMDB**; posters shown in a grid.
   User confirms / corrects / removes each match. Manual "add a movie" search box.
5. **Publish** — approved set becomes a **PunchPlay** personal list. Then buttons:
   **Add to Radarr** (main instance) and **Create Kometa collection** (writes YAML).

---

## 2. Components

Single Flask app + a background worker thread (mirrors the recipe extractor, which runs
Flask + threads in one container). Jobs are async because ingestion (download +
transcription + LLM) takes 10–60s.

```
reel-scout (one container)
├── Flask web UI + JSON API          (port 5053)
├── Ingestion pipeline               (yt-dlp, requests+bs4/trafilatura, whisper/LLM STT)
├── Extraction pipeline              (LLM via copilot-api → title JSON)
├── Matcher                          (TMDB search → poster + tmdb_id + year)
├── Publishers                       (PunchPlay / Radarr / Kometa)
└── SQLite state                     (jobs, candidates, matches)
```

Concurrency: a simple in-process job queue (`queue.Queue` + worker thread) is enough for
single-user home use — no Redis/Celery. Job status polled by the UI (`GET /job/<id>`).

---

## 3. Pipeline detail

### 3a. Ingestion (link → raw text + media)
| Source | How |
|---|---|
| Instagram post/reel, TikTok, YouTube | `yt-dlp` — grabs caption/description + video; `--write-info-json` for metadata |
| Telegram post (`t.me/...`) | fetch public web preview HTML; fall back to `yt-dlp` for embedded video |
| Generic website | `trafilatura` (clean main-content extraction) with `requests` + `bs4` fallback |
| Images in post | download via yt-dlp/gallery-dl; optional TMDB match by vision LLM/OCR |
| Video audio | extract audio (ffmpeg via yt-dlp `-x`), transcribe. **STT options** (see §7) |

| List page (Wikipedia etc., `web_list` type) | HTML fetch (Camoufox fallback); a URL `#fragment` scopes parsing to that heading's section; tables are flattened to `cell \| cell` rows |

Output = one consolidated `source_text` blob + list of downloaded media paths.

### 3b. Title extraction (LLM)
Prompt the LLM (copilot-api, OpenAI-compatible) to return **strict JSON**:
```json
{ "movies": [ { "title": "Blade Runner", "year": 1982, "confidence": 0.9 }, ... ] }
```
System prompt constrains it to *movies only* and to prefer canonical English titles.
`response_format: {"type":"json_object"}` where supported; otherwise parse defensively.
List pages use a separate "extract every row" prompt and are chunked (~6k chars) so long
tables are not truncated by the model's output budget.

### 3c. Matching (TMDB)
For each candidate: `GET /3/search/movie?query=&year=` → take best hit → store
`tmdb_id`, `title`, `release_year`, `poster_path`, `overview`. Ambiguous results keep the
top 3 as alternates for the approval UI. Poster URL: `https://image.tmdb.org/t/p/w342{poster_path}`.

### 3d. Approval
Grid of poster cards (approved/needs-review/rejected). Per card: swap to an alternate,
remove, or search-and-replace. "Add movie" search box hits `/api/tmdb/search`.

### 3e. Publish
- **PunchPlay** — create a personal list, add approved movies by `tmdb_id`.
- **Radarr** — `POST /api/v3/movie` for each (with quality profile + root folder +
  `addOptions.searchForMovie`). Skips ones already present.
- **Kometa** — write `config/reel-scout-<slug>.yml` using the `tmdb_movie` builder
  (list of tmdb ids) and register it in `config.yml` under `libraries → Films`.

---

## 4. External integrations & where secrets live

**Never hardcode.** Follow `vaultwarden-secrets` + `.env` (git-ignored) convention.

| Integration | Endpoint | Auth / secret source |
|---|---|---|
| LLM | `http://copilot-api:4141/v1` (LAN `:4141`) | key is `dummy`; join copilot-api's network or use host IP. Model e.g. `gpt-4o` |
| TMDB | `https://api.themoviedb.org/3` | reuse existing key from Kometa config (`_volumes/kometa/config/config.yml → tmdb.apikey`). Put in `.env` as `TMDB_API_KEY` |
| PunchPlay | `https://punchplay.tv/api/platform/v1` | **new** — register app at punchplay.tv/developers → `PUNCHPLAY_CLIENT_ID`/`PUNCHPLAY_CLIENT_SECRET` + device-code OAuth (scope `lists:write`) → token cached in DB. Store secrets in Vaultwarden |
| Radarr | `http://<HOST_IP>:7878` (or shared docker net `http://radarr:7878`) | `RADARR_API_KEY` from `_volumes/radarr/configs/config.xml` `<ApiKey>`. Store in Vaultwarden/`.env` |
| Kometa | writes files to `/home/lestar/_volumes/kometa/config/` | bind-mount that dir into reel-scout (rw). Kometa picks up on its next 6h ofelia run (or trigger manually) |

Networking note: copilot-api and radarr live in **separate** compose projects. Simplest is
to attach reel-scout to an existing shared external docker network, or reach them via the
host LAN IP. Decision needed at build time (see §10 open questions).

---

## 5. Data model (SQLite)

```
Job(id, source_url, list_name, status, stage, source_text, error, created_at)
Candidate(id, job_id, raw_title, year_hint, confidence)          # LLM output
Match(id, candidate_id, tmdb_id, title, year, poster_path,
      overview, state[approved|review|rejected], alternates_json)
Publish(id, job_id, target[punchplay|radarr|kometa], status, detail, created_at)
Schedule(id, name, source_url, content_type, interval_hours, enabled, auto_approve,
         publish_tmdb, publish_radarr, publish_kometa, job_id, last_run_at,
         next_run_at, last_status, last_message, last_added)
```

`status` values: `queued → ingesting → extracting → matching → awaiting_approval →
publishing → done | error`.

---

## 6. HTTP surface

UI (server-rendered, same `render_template_string` style as recipe extractor, or a small
static SPA):
- `GET  /`                     — paste form
- `GET  /job/<id>`             — progress + approval screen
- API:
- `POST /api/jobs`             — `{url, list_name}` → creates job, returns id
- `GET  /api/jobs/<id>`        — status + candidates/matches (UI polls)
- `GET  /api/tmdb/search?q=`   — manual movie search
- `POST /api/jobs/<id>/matches` — save approval decisions
- `POST /api/jobs/<id>/punchplay` — create a PunchPlay list
- `POST /api/jobs/<id>/radarr` — add approved to Radarr
- `POST /api/jobs/<id>/kometa` — write+register Kometa collection
- PunchPlay device auth: `POST /api/punchplay/device`, `POST /api/punchplay/device/poll`

---

## 6b. Scheduled parsings

UI at `/schedules` (also reachable via the **Schedule** button on a list page). A schedule
re-parses a source every `interval_hours` (daily / weekly / monthly / custom) into **one
persistent job**, so the list grows over time instead of being recreated:

- A scheduler thread checks every `SCHEDULER_TICK_SECONDS` (default 60) for due, enabled
  schedules and puts them on the same worker queue as one-off jobs.
- Each run ingests → extracts → matches, then **merges**: raw titles seen in earlier runs are
  skipped (so rejections and manual swaps stick) and TMDB ids already in the list are deduped.
- New matches are auto-approved (confidence ≥ 0.6) when `auto_approve` is on, else left for review.
- Optional auto-publish: TMDB list re-sync and Kometa collection rewrite when new approved
  titles arrive (or the target never succeeded); Radarr list registered once (it re-reads the TMDB list).
- Runs queued/running during a restart are reset and retried on the next tick.

API: `GET/POST /api/schedules`, `PATCH/DELETE /api/schedules/<id>`, `POST /api/schedules/<id>/run`.
Example: `https://en.wikipedia.org/wiki/Grand_Prix_(Cannes_Film_Festival)#Winners` weekly.
A scheduled run that extracts zero titles is recorded as an error (broken page/parser/LLM).

## 6c. Monitoring

`GET /metrics` (Prometheus format), scraped by Alloy (`job="collecta"`, `192.168.0.237:5053`).
- Counters: `collecta_parse_runs_total{kind,outcome}` (ok/no_text/no_titles/error),
  `collecta_titles_total{kind,result}` (matched/unmatched), `collecta_llm_requests_total{op,result}`,
  `collecta_tmdb_requests_total{result}`, `collecta_publish_total{target,result}`,
  `collecta_schedule_runs_total{status}`; histogram `collecta_stage_duration_seconds{stage}`.
- Gauges read from SQLite on each scrape (restart-safe), labelled `schedule`/`name`:
  `collecta_schedule_{enabled,running,last_run_success,last_run_timestamp_seconds,
  next_run_timestamp_seconds,interval_seconds,last_extracted_titles,known_titles,last_new_titles}`,
  `collecta_schedule_titles{state=approved|review|rejected|unmatched}`, plus `collecta_jobs{status}`
  and `collecta_queue_depth`.
- Alert rules live in `_volumes/prometheus/configs/collecta.yml` (Alertmanager → Discord).

## 7. Speech-to-text options (pick at build)
1. **whisper.cpp** — repo already has `whisper.cpp/` and a `whisper-asr` volume. Reuse a
   local ASR endpoint → no extra cost, no data leaves the box. **Preferred.**
2. LLM audio transcription (Gemini) as used by the recipe extractor — needs a Gemini key.
3. `faster-whisper` in-container — heavier image.

Recommendation: call the existing local Whisper ASR service if reachable; otherwise bundle
`faster-whisper` (base model) in the image.

---

## 8. Tech stack
- Python 3.12, Flask + flask-cors, SQLAlchemy (SQLite).
- `yt-dlp`, `ffmpeg`, `trafilatura`, `beautifulsoup4`, `requests`.
- `openai` client pointed at copilot-api base URL.
- Optional `faster-whisper` (or HTTP to local whisper-asr).
- Frontend: vanilla HTML/CSS/JS (poster grid), matching the repo's lightweight style.

---

## 9. Deployment

`containers/reel-scout/compose.yaml` (repo convention):
```yaml
services:
  reel-scout:
    build: .
    container_name: reel-scout
    env_file: [.env]                     # git-ignored; secrets from Vaultwarden
    ports: ["5053:5000"]
    restart: unless-stopped
    volumes:
      - /home/lestar/_volumes/reel-scout/data:/data          # sqlite + downloads
      - /home/lestar/_volumes/kometa/config:/kometa-config    # to write collection YAML
    # networks: attach to shared net for copilot-api/radarr, OR use host IPs
```
- Config/data under `/home/lestar/_volumes/reel-scout/` (current repo convention).
- Port **5053** (5050/5051 = recipe extractor, 7878 = radarr, 8084 = openwebui, 4141 = copilot-api).

---

## 10. Resolved decisions (built)
1. **Networking**: host LAN IP `192.168.0.237` for copilot-api (`:4141`), radarr (`:7878`),
   whisper-asr (`:9000`). No shared docker network needed.
2. **LLM model**: `gemini-3.8-flash` via copilot-api (multimodal; `gemini-3.7-flash` was retired by the proxy).
3. **PunchPlay OAuth**: device-code flow (UI shows code + verification URL, polls for token).
4. **STT**: reuse local whisper-asr (`onerahmet/openai-whisper-asr-webservice`, `POST /asr`).
5. **Radarr**: quality profile `2160p Balanced` (resolved by name), root `/media/Movies`,
   `searchForMovie: true`, skips titles already in the library.
6. **Kometa**: writes `config/reel-scout-<slug>.yml` (`tmdb_movie` builder) and auto-inserts
   a `- file:` line under `libraries → Films` in `config.yml` (idempotent, preserves comments).
7. **Vision/OCR**: enabled — post images are sent to the multimodal model alongside text.

### Remaining action item
- **PunchPlay credentials**: register an app at https://punchplay.tv/developers and set
  `PUNCHPLAY_CLIENT_ID` / `PUNCHPLAY_CLIENT_SECRET` in `.env` (store in Vaultwarden). Until then
  the PunchPlay button is disabled; Radarr, Kometa, and the whole extraction flow already work.

### Validated
- Image builds; app serves on `:5053`; TMDB search, LLM extraction (structured JSON with
  correct years), and copilot-api model all confirmed working end-to-end.

---

## 11. Build phases
1. **Skeleton** — compose, Dockerfile, Flask app, SQLite models, job queue, paste UI.
2. **Ingestion** — yt-dlp + website + Telegram; audio→text.
3. **Extraction + TMDB match** — LLM JSON → TMDB posters.
4. **Approval UI** — poster grid + manual search/replace.
5. **PunchPlay** — device-code OAuth + list create/populate.
6. **Radarr + Kometa** buttons.
7. Polish: error handling, retries, dedupe, "already in library" flags.
