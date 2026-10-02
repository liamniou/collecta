"""Flask app: web UI + JSON API, backed by an in-process job worker."""
import os
import queue
import shutil
import threading
import traceback
from datetime import datetime

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from . import config, content_kb, extract, ingest, models, tmdb
from .models import (
    APPROVED, AWAITING_APPROVAL, DONE, EXTRACTING, INGESTING, MATCHING, REJECTED,
    REVIEW, Job, Match, Publish, get_session,
)
from .publishers import kometa, radarr, tmdb_list

app = Flask(__name__, template_folder="templates")
CORS(app)

_job_queue: "queue.Queue[str]" = queue.Queue()


# ---------------------------------------------------------------- worker

def start_worker() -> None:
    threading.Thread(target=_worker_loop, daemon=True).start()


def _worker_loop() -> None:
    while True:
        job_id = _job_queue.get()
        try:
            _process_job(job_id)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            _fail(job_id, traceback.format_exc(limit=3))
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
    with get_session() as session:
        job = session.get(Job, job_id)
        url, list_name = job.source_url, job.list_name
        content_type = job.content_type or "auto"

    work_dir = os.path.join(config.JOBS_DIR, str(job_id))

    _set_stage(job_id, INGESTING, "Downloading & transcribing")
    ingested = ingest.ingest(url, work_dir, content_type)

    with get_session() as session:
        job = session.get(Job, job_id)
        job.source_text = ingested["source_text"]

    _set_stage(job_id, EXTRACTING, "Finding movie titles")
    candidates = extract.extract_titles(ingested["source_text"], ingested["image_paths"])

    _set_stage(job_id, MATCHING, "Matching posters on TMDB")
    with get_session() as session:
        job = session.get(Job, job_id)
        seen_tmdb: set[int] = set()
        seen_unmatched: set[str] = set()
        for cand in candidates:
            session.add(models.Candidate(
                job_id=job_id, raw_title=cand["title"],
                year_hint=cand["year"], confidence=cand["confidence"],
            ))
            matched = tmdb.match(cand)
            if not matched:
                # Keep the title visible with a placeholder so the user can resolve it.
                key = cand["title"].strip().lower()
                if key in seen_unmatched:
                    continue
                seen_unmatched.add(key)
                session.add(Match(
                    job_id=job_id, tmdb_id=None, title=cand["title"],
                    year=cand["year"], poster_path="", overview="", state=REVIEW,
                    alternates_json="[]",
                ))
                continue
            best = matched["best"]
            if best["tmdb_id"] in seen_tmdb:
                continue
            seen_tmdb.add(best["tmdb_id"])
            state = APPROVED if cand["confidence"] >= 0.6 else REVIEW
            session.add(Match(
                job_id=job_id, tmdb_id=best["tmdb_id"], title=best["title"],
                year=best["year"], poster_path=best["poster_path"],
                overview=best["overview"], state=state,
                alternates_json=models.json.dumps(matched["alternates"]),
            ))

    # Suggest a list name from the analysis (before flipping to review so it's never blank).
    with get_session() as session:
        job = session.get(Job, job_id)
        if not (job.list_name or "").strip():
            titles = [m.title for m in job.matches]
            job.list_name = extract.suggest_list_name(job.source_text, titles)

    _set_stage(job_id, AWAITING_APPROVAL, "Review the matches")
    # Downloaded media is no longer needed once matching is done.
    shutil.rmtree(work_dir, ignore_errors=True)


def _approved_movies(session, job_id: str) -> list[dict]:
    matches = session.query(Match).filter_by(job_id=job_id, state=APPROVED).all()
    return [
        {"tmdb_id": m.tmdb_id, "title": m.title, "year": m.year, "poster_path": m.poster_path}
        for m in matches
    ]


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
    _job_queue.put(job_id)
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
    with get_session() as session:
        job = session.get(Job, job_id)
        list_name = job.list_name if job else "Collecta list"
        tmdb_list_id = (job.tmdb_list_id if job else "") or ""
    if not tmdb_list_id:
        return jsonify({"error": "Create the TMDB list first, then add it to Radarr."}), 400
    try:
        detail = radarr.add_tmdb_list(tmdb_list_id, list_name)
        status = "ok"
    except Exception as exc:  # noqa: BLE001
        detail, status = str(exc), "error"
    with get_session() as session:
        _record_publish(session, job_id, "radarr", status, detail)
    code = 200 if status == "ok" else 400
    return jsonify({"status": status, "detail": detail}), code


@app.route("/api/jobs/<job_id>/kometa", methods=["POST"])
def publish_kometa(job_id: str):
    with get_session() as session:
        movies = _approved_movies(session, job_id)
        job = session.get(Job, job_id)
        list_name = job.list_name if job else "Collecta list"
        source_url = job.source_url if job else ""
        tmdb_list_id = (job.tmdb_list_id if job else "") or ""
    if not movies:
        return jsonify({"error": "No approved movies."}), 400
    try:
        detail = kometa.create_collection(list_name, movies, source_url, tmdb_list_id)
        detail += " " + kometa.trigger_run(list_name)
        status = "ok"
    except Exception as exc:  # noqa: BLE001
        detail, status = str(exc), "error"
    with get_session() as session:
        _record_publish(session, job_id, "kometa", status, detail)
    code = 200 if status == "ok" else 400
    return jsonify({"status": status, "detail": detail}), code


@app.route("/api/jobs/<job_id>/tmdb", methods=["POST"])
def publish_tmdb(job_id: str):
    with get_session() as session:
        movies = _approved_movies(session, job_id)
        job = session.get(Job, job_id)
        list_name = job.list_name if job else "Collecta list"
        existing = (job.tmdb_list_id if job else "") or ""
    if not movies:
        return jsonify({"error": "No approved movies."}), 400
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
    code = 200 if status == "ok" else 400
    return jsonify({"status": status, "detail": detail}), code


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
