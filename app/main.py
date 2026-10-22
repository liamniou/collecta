"""Flask app: web UI + JSON API, backed by an in-process job worker and scheduler."""
import os
import queue
import shutil
import threading
import time
import traceback
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from . import config, content_kb, extract, ingest, models, tmdb
from .models import (
    APPROVED, AWAITING_APPROVAL, DONE, EXTRACTING, INGESTING, MATCHING, REJECTED,
    REVIEW, Job, Match, Publish, Schedule, get_session,
)
from .publishers import kometa, radarr, tmdb_list

app = Flask(__name__, template_folder="templates")
CORS(app)

# Items are (kind, id): ("job", job_id) for one-off parsings, ("schedule", schedule_id) for scheduled ones.
_job_queue: "queue.Queue[tuple[str, str | int]]" = queue.Queue()


# ---------------------------------------------------------------- worker

def start_worker() -> None:
    _reset_interrupted_schedules()
    threading.Thread(target=_worker_loop, daemon=True).start()
    threading.Thread(target=_scheduler_loop, daemon=True).start()


def _worker_loop() -> None:
    while True:
        kind, item_id = _job_queue.get()
        try:
            if kind == "schedule":
                _run_schedule(int(item_id))
            else:
                _process_job(str(item_id))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            if kind == "schedule":
                _finish_schedule(int(item_id), "error", traceback.format_exc(limit=3), 0)
            else:
                _fail(str(item_id), traceback.format_exc(limit=3))
        finally:
            _job_queue.task_done()


def _set_stage(job_id: str, status: str, stage: str) -> None:
    with get_session() as session:
        job = session.get(Job, job_id)
        if job:
            job.status = status
            job.stage = stage


def _fail(job_id: str, message: str) -> None:
    with get_session() as session:
        job = session.get(Job, job_id)
        if job:
            job.status = models.ERROR
            job.error = message


def _process_job(job_id: str) -> None:
    _parse_into_job(job_id, fresh=True, auto_approve=True)


def _parse_into_job(job_id: str, fresh: bool, auto_approve: bool) -> tuple[int, int]:
    """Ingest → extract → match, merging only titles not seen in earlier runs of this job.

    `fresh` jobs show stage progress and end in review; re-runs of an existing job stay
    silent so the review page keeps working while a scheduled parse is in progress.
    Returns (new matches, new approved matches).
    """
    with get_session() as session:
        job = session.get(Job, job_id)
        url = job.source_url
        content_type = job.content_type or "auto"

    def stage(status: str, text: str) -> None:
        if fresh:
            _set_stage(job_id, status, text)

    work_dir = os.path.join(config.JOBS_DIR, str(job_id))
    list_mode = content_kb.strategy_for(content_type) == "list_page"

    stage(INGESTING, "Fetching the list page" if list_mode else "Downloading & transcribing")
    ingested = ingest.ingest(url, work_dir, content_type)

    with get_session() as session:
        job = session.get(Job, job_id)
        job.source_text = ingested["source_text"]

    stage(EXTRACTING, "Finding movie titles")
    candidates = extract.extract_titles(
        ingested["source_text"], ingested["image_paths"], list_mode=list_mode
    )

    stage(MATCHING, "Matching posters on TMDB")
    added = added_approved = 0
    with get_session() as session:
        job = session.get(Job, job_id)
        # Titles from previous runs are never re-added, so user rejections/swaps stick.
        seen_raw = {(c.raw_title or "").strip().lower() for c in job.candidates}
        seen_tmdb = {m.tmdb_id for m in job.matches if m.tmdb_id}
        seen_unmatched = {(m.title or "").strip().lower() for m in job.matches if not m.tmdb_id}
        for cand in candidates:
            raw_key = cand["title"].strip().lower()
            if raw_key in seen_raw:
                continue
            seen_raw.add(raw_key)
            session.add(models.Candidate(
                job_id=job_id, raw_title=cand["title"],
                year_hint=cand["year"], confidence=cand["confidence"],
            ))
            matched = tmdb.match(cand)
            if not matched:
                # Keep the title visible with a placeholder so the user can resolve it.
                if raw_key in seen_unmatched:
                    continue
                seen_unmatched.add(raw_key)
                session.add(Match(
                    job_id=job_id, tmdb_id=None, title=cand["title"],
                    year=cand["year"], poster_path="", overview="", state=REVIEW,
                    alternates_json="[]",
                ))
                added += 1
                continue
            best = matched["best"]
            if best["tmdb_id"] in seen_tmdb:
                continue
            seen_tmdb.add(best["tmdb_id"])
            state = APPROVED if auto_approve and cand["confidence"] >= 0.6 else REVIEW
            session.add(Match(
                job_id=job_id, tmdb_id=best["tmdb_id"], title=best["title"],
                year=best["year"], poster_path=best["poster_path"],
                overview=best["overview"], state=state,
                alternates_json=models.json.dumps(matched["alternates"]),
            ))
            added += 1
            added_approved += state == APPROVED

    # Suggest a list name from the analysis (before flipping to review so it's never blank).
    with get_session() as session:
        job = session.get(Job, job_id)
        if not (job.list_name or "").strip():
            titles = [m.title for m in job.matches]
            job.list_name = extract.suggest_list_name(job.source_text, titles)

    stage(AWAITING_APPROVAL, "Review the matches")
    # Downloaded media is no longer needed once matching is done.
    shutil.rmtree(work_dir, ignore_errors=True)
    return added, added_approved


# ---------------------------------------------------------------- scheduler

_ACTIVE = ("queued", "running")


def _scheduler_loop() -> None:
    while True:
        try:
            _enqueue_due_schedules()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(config.SCHEDULER_TICK_SECONDS)


def _enqueue_due_schedules() -> None:
    now = datetime.utcnow()
    with get_session() as session:
        due = [
            s for s in session.query(Schedule).filter(Schedule.enabled.is_(True)).all()
            if s.next_run_at and s.next_run_at <= now and (s.last_status or "") not in _ACTIVE
        ]
        for s in due:
            s.last_status = "queued"
        ids = [s.id for s in due]
    for sid in ids:
        _job_queue.put(("schedule", sid))


def _reset_interrupted_schedules() -> None:
    """The queue is in-memory, so runs queued/running at shutdown must be re-picked."""
    with get_session() as session:
        for s in session.query(Schedule).filter(Schedule.last_status.in_(_ACTIVE)).all():
            s.last_status = "error"
            s.last_message = "Interrupted by a restart; will retry."
            s.next_run_at = datetime.utcnow()


def _finish_schedule(schedule_id: int, status: str, message: str, added: int) -> None:
    with get_session() as session:
        s = session.get(Schedule, schedule_id)
        if not s:
            return
        now = datetime.utcnow()
        s.last_run_at = now
        s.next_run_at = now + timedelta(hours=max(1, s.interval_hours or 24))
        s.last_status = status
        s.last_message = message
        s.last_added = added


def _run_schedule(schedule_id: int) -> None:
    with get_session() as session:
        s = session.get(Schedule, schedule_id)
        if not s:
            return
        s.last_status = "running"
        s.last_message = "Parsing…"
        job_id = s.job_id or models.job_id_for_url(s.source_url, s.content_type)
        job = session.get(Job, job_id)
        if not job:
            job = Job(id=job_id, source_url=s.source_url, list_name=s.name or "",
                      content_type=s.content_type or "auto", status=models.QUEUED)
            session.add(job)
        s.job_id = job_id
        fresh = job.status not in (AWAITING_APPROVAL, DONE)
        auto_approve = bool(s.auto_approve)
        targets = [t for t, on in (("tmdb", s.publish_tmdb), ("radarr", s.publish_radarr),
                                   ("kometa", s.publish_kometa)) if on]

    try:
        added, added_approved = _parse_into_job(job_id, fresh=fresh, auto_approve=auto_approve)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        if fresh:
            _fail(job_id, traceback.format_exc(limit=3))
        _finish_schedule(schedule_id, "error", f"Parsing failed: {exc}", 0)
        return

    notes = [f"{added} new title(s)" + (f", {added_approved} auto-approved" if added else "")]
    status = "ok"
    with get_session() as session:
        job = session.get(Job, job_id)
        published = {p.target for p in job.publishes if p.status == "ok"}
    for target in targets:
        # Radarr only needs registering once (it re-reads the TMDB list itself);
        # TMDB/Kometa are re-synced whenever new approved titles arrived.
        if target in published and (target == "radarr" or not added_approved):
            continue
        result, detail = _PUBLISHERS[target](job_id)
        notes.append(f"{target}: {detail}")
        if result == "error":
            status = "error"
    _finish_schedule(schedule_id, status, " · ".join(notes), added)


# ---------------------------------------------------------------- publishing

def _approved_movies(session, job_id: str) -> list[dict]:
    matches = session.query(Match).filter_by(job_id=job_id, state=APPROVED).all()
    return [
        {"tmdb_id": m.tmdb_id, "title": m.title, "year": m.year, "poster_path": m.poster_path}
        for m in matches
    ]


def _do_publish_tmdb(job_id: str) -> tuple[str, str]:
    """Returns (status, detail): status is ok | error | skip (nothing recorded on skip)."""
    with get_session() as session:
        movies = _approved_movies(session, job_id)
        job = session.get(Job, job_id)
        list_name = job.list_name if job else "Collecta list"
        existing = (job.tmdb_list_id if job else "") or ""
    if not movies:
        return "skip", "No approved movies."
    try:
        result = tmdb_list.create_or_update_list(list_name, movies, existing or None)
        detail = f"Synced {result['added']} movie(s) to TMDB list {result['id']}: {result['url']}"
        status = "ok"
        with get_session() as session:
            job = session.get(Job, job_id)
            if job:
                job.tmdb_list_id = result["id"]
    except Exception as exc:  # noqa: BLE001
        detail, status = str(exc), "error"
    with get_session() as session:
        _record_publish(session, job_id, "tmdb", status, detail)
    return status, detail


def _do_publish_radarr(job_id: str) -> tuple[str, str]:
    with get_session() as session:
        job = session.get(Job, job_id)
        list_name = job.list_name if job else "Collecta list"
        tmdb_list_id = (job.tmdb_list_id if job else "") or ""
    if not tmdb_list_id:
        return "skip", "Create the TMDB list first, then add it to Radarr."
    try:
        detail = radarr.add_tmdb_list(tmdb_list_id, list_name)
        status = "ok"
    except Exception as exc:  # noqa: BLE001
        detail, status = str(exc), "error"
    with get_session() as session:
        _record_publish(session, job_id, "radarr", status, detail)
    return status, detail


def _do_publish_kometa(job_id: str) -> tuple[str, str]:
    with get_session() as session:
        movies = _approved_movies(session, job_id)
        job = session.get(Job, job_id)
        list_name = job.list_name if job else "Collecta list"
        source_url = job.source_url if job else ""
        tmdb_list_id = (job.tmdb_list_id if job else "") or ""
    if not movies:
        return "skip", "No approved movies."
    try:
        detail = kometa.create_collection(list_name, movies, source_url, tmdb_list_id)
        detail += " " + kometa.trigger_run(list_name)
        status = "ok"
    except Exception as exc:  # noqa: BLE001
        detail, status = str(exc), "error"
    with get_session() as session:
        _record_publish(session, job_id, "kometa", status, detail)
    return status, detail


_PUBLISHERS = {"tmdb": _do_publish_tmdb, "radarr": _do_publish_radarr, "kometa": _do_publish_kometa}


def _publish_response(status: str, detail: str):
    if status == "skip":
        return jsonify({"error": detail}), 400
    return jsonify({"status": status, "detail": detail}), (200 if status == "ok" else 400)


# ---------------------------------------------------------------- serialization

def _job_dict(session, job: Job) -> dict:
    return {
        "id": job.id,
        "source_url": job.source_url,
        "list_name": job.list_name,
        "status": job.status,
        "stage": job.stage,
        "error": job.error,
        "content_type": job.content_type or "auto",
        "tmdb_list_id": job.tmdb_list_id or "",
        "radarr_status": models.json.loads(job.radarr_status) if job.radarr_status else {},
        "radarr_checked_at": job.radarr_checked_at.isoformat() if job.radarr_checked_at else "",
        "matches": [
            {
                "id": m.id,
                "tmdb_id": m.tmdb_id,
                "title": m.title,
                "year": m.year,
                "poster_url": tmdb.poster_url(m.poster_path),
                "overview": m.overview,
                "state": m.state,
                "alternates": [
                    {**a, "poster_url": tmdb.poster_url(a.get("poster_path", ""))}
                    for a in m.alternates
                ],
            }
            # Unmatched (no TMDB id) first so they are easy to resolve.
            for m in sorted(job.matches, key=lambda x: (x.tmdb_id is not None, x.id))
        ],
        "publishes": [
            {"target": p.target, "status": p.status, "detail": p.detail}
            for p in sorted(job.publishes, key=lambda x: x.id)
        ],
        "schedule": next((
            {"id": s.id, "interval_hours": s.interval_hours, "enabled": bool(s.enabled),
             "next_run_at": _iso(s.next_run_at), "last_run_at": _iso(s.last_run_at)}
            for s in session.query(Schedule).filter_by(job_id=job.id).limit(1)
        ), None),
    }


def _record_publish(session, job_id: str, target: str, status: str, detail: str) -> None:
    session.add(Publish(job_id=job_id, target=target, status=status, detail=detail))


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/job/<job_id>")
def job_page(job_id: str):
    return render_template("job.html", job_id=job_id, radarr_web_url=config.RADARR_WEB_URL)


@app.route("/api/content-types")
def content_types():
    url = (request.args.get("url") or "").strip()
    return jsonify({
        "types": content_kb.list_types(),
        "suggested": content_kb.detect(url) if url else "auto",
    })


@app.route("/api/jobs", methods=["POST"])
def create_job():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    text = (data.get("text") or "").strip()
    if not url and not text:
        return jsonify({"error": "url or text is required"}), 400
    list_name = (data.get("list_name") or "").strip()  # optional; suggested after analysis
    content_type = (data.get("content_type") or "auto").strip() or "auto"
    if text and not url:
        # Pasted text has no link to fetch; store the text itself as the source.
        url, content_type = text, "pasted_text"
    force = bool(data.get("force"))
    job_id = models.job_id_for_url(url, content_type)
    with get_session() as session:
        existing = session.get(Job, job_id)
        if existing and not force:
            return jsonify({"id": job_id, "cached": True})
        if existing:
            # Force rescan: drop the old result and reprocess in place.
            session.delete(existing)
            session.flush()
        session.add(Job(id=job_id, source_url=url, list_name=list_name,
                        content_type=content_type, status=models.QUEUED))
    shutil.rmtree(os.path.join(config.JOBS_DIR, job_id), ignore_errors=True)
    _job_queue.put(("job", job_id))
    return jsonify({"id": job_id})


@app.route("/api/jobs/recent")
def recent_jobs():
    with get_session() as session:
        jobs = session.query(Job).order_by(Job.created_at.desc()).limit(5).all()
        return jsonify({"jobs": [
            {
                "id": j.id,
                "source_url": j.source_url,
                "list_name": j.list_name,
                "status": j.status,
                "count": len(j.matches),
                "content_type": j.content_type or "auto",
            }
            for j in jobs
        ]})


@app.route("/api/jobs/<job_id>")
def get_job(job_id: str):
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        return jsonify(_job_dict(session, job))


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id: str):
    shutil.rmtree(os.path.join(config.JOBS_DIR, str(job_id)), ignore_errors=True)
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        session.delete(job)  # cascades to candidates/matches/publishes
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/kometa-run", methods=["POST"])
def kometa_run(job_id: str):
    with get_session() as session:
        job = session.get(Job, job_id)
        list_name = job.list_name if job else ""
    if not list_name:
        return jsonify({"error": "no list name"}), 400
    return jsonify({"detail": kometa.trigger_run(list_name)})


@app.route("/api/jobs/<job_id>/name", methods=["POST"])
def update_name(job_id: str):
    name = (request.get_json(force=True).get("list_name") or "").strip()
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        if name:
            job.list_name = name
        return jsonify({"list_name": job.list_name})


@app.route("/api/tmdb/search")
def tmdb_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})
    results = tmdb.search(q, limit=10, include_adult=True)
    for r in results:
        r["poster_url"] = tmdb.poster_url(r["poster_path"])
    return jsonify({"results": results})


@app.route("/api/jobs/<job_id>/matches", methods=["POST"])
def update_matches(job_id: str):
    data = request.get_json(force=True)
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        for upd in data.get("updates", []):
            m = session.get(Match, upd.get("id"))
            if not m or m.job_id != job_id:
                continue
            if "state" in upd:
                m.state = upd["state"]
            for field in ("tmdb_id", "title", "year", "poster_path", "overview"):
                if field in upd and upd[field] is not None:
                    setattr(m, field, upd[field])
        for add in data.get("added", []):
            if not add.get("tmdb_id"):
                continue
            session.add(Match(
                job_id=job_id, tmdb_id=add["tmdb_id"], title=add.get("title"),
                year=add.get("year"), poster_path=add.get("poster_path", ""),
                overview=add.get("overview", ""), state=APPROVED,
            ))
        session.flush()
        return jsonify(_job_dict(session, job))


@app.route("/api/jobs/<job_id>/radarr-status")
def radarr_status(job_id: str):
    with get_session() as session:
        matches = session.query(Match).filter_by(job_id=job_id).all()
        ids = [m.tmdb_id for m in matches if m.tmdb_id]
    try:
        statuses = radarr.library_status(ids)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    payload = {str(k): v for k, v in statuses.items()}
    checked_at = datetime.utcnow()
    with get_session() as session:
        job = session.get(Job, job_id)
        if job:
            job.radarr_status = models.json.dumps(payload)
            job.radarr_checked_at = checked_at
    return jsonify({"statuses": payload, "checked_at": checked_at.isoformat()})


@app.route("/api/jobs/<job_id>/radarr-import", methods=["POST"])
def radarr_import(job_id: str):
    try:
        detail = radarr.import_downloads()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify({"detail": detail})


@app.route("/api/jobs/<job_id>/radarr-rescue", methods=["POST"])
def radarr_rescue(job_id: str):
    with get_session() as session:
        matches = session.query(Match).filter_by(job_id=job_id).all()
        ids = [m.tmdb_id for m in matches if m.tmdb_id]
    try:
        detail = radarr.rescue_searching(ids)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify({"detail": detail})


@app.route("/api/jobs/<job_id>/radarr", methods=["POST"])
def publish_radarr(job_id: str):
    return _publish_response(*_do_publish_radarr(job_id))


@app.route("/api/jobs/<job_id>/kometa", methods=["POST"])
def publish_kometa(job_id: str):
    return _publish_response(*_do_publish_kometa(job_id))


@app.route("/api/jobs/<job_id>/tmdb", methods=["POST"])
def publish_tmdb(job_id: str):
    return _publish_response(*_do_publish_tmdb(job_id))


# ---- TMDB account authentication (v3 session) ----

@app.route("/api/tmdb/status")
def tmdb_status():
    return jsonify({"configured": tmdb_list.configured(), "authed": tmdb_list.is_authed()})


@app.route("/api/tmdb/auth", methods=["POST"])
def tmdb_auth():
    try:
        return jsonify(tmdb_list.start_auth())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


@app.route("/api/tmdb/auth/finish", methods=["POST"])
def tmdb_auth_finish():
    token = (request.get_json(force=True).get("request_token") or "").strip()
    if not token:
        return jsonify({"error": "request_token required"}), 400
    try:
        return jsonify({"authed": tmdb_list.finish_auth(token)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


# ---- Scheduled parsings ----

_SCHEDULE_FLAGS = ("enabled", "auto_approve", "publish_tmdb", "publish_radarr", "publish_kometa")


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() + "Z" if dt else ""


def _schedule_dict(session, s: Schedule) -> dict:
    job = session.get(Job, s.job_id) if s.job_id else None
    matches = job.matches if job else []
    return {
        "id": s.id,
        "name": s.name or (job.list_name if job else "") or "",
        "source_url": s.source_url,
        "content_type": s.content_type or "auto",
        "interval_hours": s.interval_hours,
        **{f: bool(getattr(s, f)) for f in _SCHEDULE_FLAGS},
        "job_id": s.job_id if job else "",
        "list_name": job.list_name if job else "",
        "tmdb_list_id": (job.tmdb_list_id or "") if job else "",
        "count": len(matches),
        "approved": sum(1 for m in matches if m.state == APPROVED),
        "review": sum(1 for m in matches if m.state == REVIEW),
        "last_run_at": _iso(s.last_run_at),
        "next_run_at": _iso(s.next_run_at),
        "last_status": s.last_status or "",
        "last_message": s.last_message or "",
        "last_added": s.last_added or 0,
    }


def _apply_schedule_fields(s: Schedule, data: dict) -> str | None:
    """Validate and copy editable fields; returns an error message or None."""
    if "url" in data:
        url = (data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return "A valid http(s) URL is required."
        s.source_url = url
    if "content_type" in data or not s.content_type:
        ctype = (data.get("content_type") or "auto").strip() or "auto"
        s.content_type = content_kb.detect(s.source_url) if ctype == "auto" else ctype
    if "name" in data:
        s.name = (data.get("name") or "").strip()[:200]
    if "interval_hours" in data:
        try:
            hours = int(data["interval_hours"])
        except (TypeError, ValueError):
            return "interval_hours must be a number."
        if not 1 <= hours <= 24 * 366:
            return "interval_hours must be between 1 and 8784."
        s.interval_hours = hours
        if s.last_run_at:
            s.next_run_at = s.last_run_at + timedelta(hours=hours)
    for flag in _SCHEDULE_FLAGS:
        if flag in data:
            setattr(s, flag, bool(data[flag]))
    return None


@app.route("/schedules")
def schedules_page():
    return render_template("schedules.html")


@app.route("/api/schedules")
def list_schedules():
    with get_session() as session:
        rows = session.query(Schedule).order_by(Schedule.created_at.desc()).all()
        return jsonify({"schedules": [_schedule_dict(session, s) for s in rows]})


@app.route("/api/schedules", methods=["POST"])
def create_schedule():
    data = request.get_json(force=True) or {}
    if not (data.get("url") or "").strip():
        return jsonify({"error": "url is required"}), 400
    data.setdefault("interval_hours", 168)
    run_now = data.get("run_now", True)
    with get_session() as session:
        s = Schedule(source_url="", content_type="", enabled=True, auto_approve=True)
        err = _apply_schedule_fields(s, data)
        if err:
            return jsonify({"error": err}), 400
        now = datetime.utcnow()
        s.next_run_at = now if run_now else now + timedelta(hours=s.interval_hours)
        s.job_id = models.job_id_for_url(s.source_url, s.content_type)
        session.add(s)
        session.flush()
        sid = s.id
    _enqueue_due_schedules()
    with get_session() as session:
        return jsonify(_schedule_dict(session, session.get(Schedule, sid))), 201


@app.route("/api/schedules/<int:schedule_id>", methods=["PATCH"])
def update_schedule(schedule_id: int):
    data = request.get_json(force=True) or {}
    with get_session() as session:
        s = session.get(Schedule, schedule_id)
        if not s:
            return jsonify({"error": "not found"}), 404
        old_key = (s.source_url, s.content_type)
        err = _apply_schedule_fields(s, data)
        if err:
            return jsonify({"error": err}), 400
        if (s.source_url, s.content_type) != old_key:
            # A different source means a different list; start it fresh on the next tick.
            s.job_id = models.job_id_for_url(s.source_url, s.content_type)
            s.next_run_at = datetime.utcnow()
        session.flush()
        return jsonify(_schedule_dict(session, s))


@app.route("/api/schedules/<int:schedule_id>", methods=["DELETE"])
def delete_schedule(schedule_id: int):
    with get_session() as session:
        s = session.get(Schedule, schedule_id)
        if not s:
            return jsonify({"error": "not found"}), 404
        session.delete(s)  # the collected list (job) is kept
    return jsonify({"ok": True})


@app.route("/api/schedules/<int:schedule_id>/run", methods=["POST"])
def run_schedule_now(schedule_id: int):
    with get_session() as session:
        s = session.get(Schedule, schedule_id)
        if not s:
            return jsonify({"error": "not found"}), 404
        if (s.last_status or "") in _ACTIVE:
            return jsonify({"error": "Already queued or running."}), 409
        s.last_status = "queued"
        s.next_run_at = datetime.utcnow()
    _job_queue.put(("schedule", schedule_id))
    return jsonify({"ok": True})
