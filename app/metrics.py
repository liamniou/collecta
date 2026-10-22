"""Prometheus metrics: in-process event counters + DB-backed gauges collected at scrape time."""
import time
from datetime import datetime

from prometheus_client import (
    CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, disable_created_metrics, generate_latest,
)
from prometheus_client.core import GaugeMetricFamily

disable_created_metrics()

PARSE_KINDS = ("job", "schedule")
# ok: titles found · no_text: nothing could be fetched · no_titles: text but no films · error: exception
PARSE_OUTCOMES = ("ok", "no_text", "no_titles", "error")

PARSE_RUNS = Counter(
    "collecta_parse_runs_total", "Parse runs (ingest → extract → match) by outcome.", ["kind", "outcome"])
TITLES = Counter(
    "collecta_titles_total", "Newly found titles by TMDB match result.", ["kind", "result"])
STAGE_SECONDS = Histogram(
    "collecta_stage_duration_seconds", "Duration of each parsing stage.", ["stage"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200))
LLM_REQUESTS = Counter(
    "collecta_llm_requests_total", "LLM calls by operation and result.", ["op", "result"])
TMDB_REQUESTS = Counter(
    "collecta_tmdb_requests_total", "TMDB movie searches by result.", ["result"])
PUBLISHES = Counter(
    "collecta_publish_total", "Publish attempts by target and result.", ["target", "result"])
SCHEDULE_RUNS = Counter(
    "collecta_schedule_runs_total", "Scheduled runs by final status.", ["status"])

# Pre-create label sets so increase()/rate() alerts see the 0 → 1 transition.
for _k in PARSE_KINDS:
    for _o in PARSE_OUTCOMES:
        PARSE_RUNS.labels(_k, _o)
    for _r in ("matched", "unmatched"):
        TITLES.labels(_k, _r)
for _op in ("extract", "extract_fallback", "extract_list", "list_name"):
    for _r in ("ok", "error"):
        LLM_REQUESTS.labels(_op, _r)
for _r in ("ok", "error", "not_configured"):
    TMDB_REQUESTS.labels(_r)
for _t in ("tmdb", "radarr", "kometa"):
    for _r in ("ok", "error"):
        PUBLISHES.labels(_t, _r)
for _s in ("ok", "error"):
    SCHEDULE_RUNS.labels(_s)


class _Timer:
    def __init__(self, stage: str):
        self.stage = stage

    def __enter__(self):
        self.start = time.monotonic()

    def __exit__(self, *exc):
        STAGE_SECONDS.labels(self.stage).observe(time.monotonic() - self.start)


def stage_timer(stage: str) -> _Timer:
    return _Timer(stage)


def _ts(dt: datetime | None) -> float:
    # Stored datetimes are naive UTC.
    return (dt - datetime(1970, 1, 1)).total_seconds() if dt else 0.0


class _StateCollector:
    """Reads schedule/job state from SQLite on every scrape (restart-safe)."""

    def __init__(self, queue_size):
        self._queue_size = queue_size

    def collect(self):
        from .models import APPROVED, REJECTED, Candidate, Job, Match, Schedule, get_session

        queue = GaugeMetricFamily("collecta_queue_depth", "Items waiting in the worker queue.")
        queue.add_metric([], self._queue_size())
        yield queue

        jobs = GaugeMetricFamily("collecta_jobs", "Lists (jobs) by status.", labels=["status"])
        labels = ["schedule", "name"]
        g = {
            "enabled": GaugeMetricFamily("collecta_schedule_enabled", "1 if the schedule is enabled.", labels=labels),
            "running": GaugeMetricFamily("collecta_schedule_running", "1 if a run is queued or in progress.", labels=labels),
            "success": GaugeMetricFamily("collecta_schedule_last_run_success", "1 if the last finished run succeeded (absent if never run).", labels=labels),
            "last": GaugeMetricFamily("collecta_schedule_last_run_timestamp_seconds", "Last finished run (unix time).", labels=labels),
            "next": GaugeMetricFamily("collecta_schedule_next_run_timestamp_seconds", "Next planned run (unix time).", labels=labels),
            "interval": GaugeMetricFamily("collecta_schedule_interval_seconds", "Configured run interval.", labels=labels),
            "extracted": GaugeMetricFamily("collecta_schedule_last_extracted_titles", "Titles extracted from the source on the last run.", labels=labels),
            "known": GaugeMetricFamily("collecta_schedule_known_titles", "Distinct source titles seen across all runs.", labels=labels),
            "added": GaugeMetricFamily("collecta_schedule_last_new_titles", "New titles added on the last run.", labels=labels),
            "titles": GaugeMetricFamily("collecta_schedule_titles", "Titles in the scheduled list by state.", labels=labels + ["state"]),
        }
        with get_session() as session:
            for status, count in _count_by(session, Job.status):
                jobs.add_metric([status or "unknown"], count)
            for s in session.query(Schedule).all():
                job = session.get(Job, s.job_id) if s.job_id else None
                lv = [str(s.id), s.name or (job.list_name if job else "") or s.source_url]
                g["enabled"].add_metric(lv, 1 if s.enabled else 0)
                g["running"].add_metric(lv, 1 if s.last_status in ("queued", "running") else 0)
                if s.last_status in ("ok", "error"):
                    g["success"].add_metric(lv, 1 if s.last_status == "ok" else 0)
                if s.last_run_at:
                    g["last"].add_metric(lv, _ts(s.last_run_at))
                if s.next_run_at:
                    g["next"].add_metric(lv, _ts(s.next_run_at))
                g["interval"].add_metric(lv, (s.interval_hours or 0) * 3600)
                g["extracted"].add_metric(lv, s.last_extracted or 0)
                g["added"].add_metric(lv, s.last_added or 0)
                known = session.query(Candidate).filter_by(job_id=s.job_id).count() if job else 0
                g["known"].add_metric(lv, known)
                counts = {"approved": 0, "review": 0, "rejected": 0, "unmatched": 0}
                for m in session.query(Match).filter_by(job_id=s.job_id).all() if job else []:
                    if not m.tmdb_id and m.state != REJECTED:
                        counts["unmatched"] += 1
                    elif m.state == APPROVED:
                        counts["approved"] += 1
                    elif m.state == REJECTED:
                        counts["rejected"] += 1
                    else:
                        counts["review"] += 1
                for state, n in counts.items():
                    g["titles"].add_metric(lv + [state], n)
        yield jobs
        yield from g.values()


def _count_by(session, column):
    from sqlalchemy import func

    return session.query(column, func.count()).group_by(column).all()


_registered = False


def register_state_collector(queue_size) -> None:
    global _registered
    if not _registered:
        REGISTRY.register(_StateCollector(queue_size))
        _registered = True


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
