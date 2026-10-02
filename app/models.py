"""SQLAlchemy models and session helpers."""
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from . import config

Base = declarative_base()

# Job lifecycle
QUEUED = "queued"
INGESTING = "ingesting"
EXTRACTING = "extracting"
MATCHING = "matching"
AWAITING_APPROVAL = "awaiting_approval"
DONE = "done"
ERROR = "error"

# Match states
APPROVED = "approved"
REVIEW = "review"
REJECTED = "rejected"


def job_id_for_url(url: str, content_type: str = "auto") -> str:
    """Deterministic short id from URL (+ non-auto content type) so each maps to one job."""
    key = (url or "").strip()
    if content_type and content_type != "auto":
        key += "|" + content_type
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String(16), primary_key=True)
    source_url = Column(Text, nullable=False)
    list_name = Column(String(200))
    status = Column(String(40), default=QUEUED, index=True)
    stage = Column(String(200), default="")
    source_text = Column(Text, default="")
    error = Column(Text, default="")
    content_type = Column(String(30), default="auto")
    tmdb_list_id = Column(String(30), default="")
    radarr_status = Column(Text, default="")  # cached JSON map tmdb_id -> state
    radarr_checked_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    candidates = relationship("Candidate", back_populates="job", cascade="all, delete-orphan")
    matches = relationship("Match", back_populates="job", cascade="all, delete-orphan")
    publishes = relationship("Publish", back_populates="job", cascade="all, delete-orphan")


class Candidate(Base):
    __tablename__ = "candidates"

    id = Column(Integer, primary_key=True)
    job_id = Column(String(16), ForeignKey("jobs.id"), index=True)
    raw_title = Column(String(400))
    year_hint = Column(Integer)
    confidence = Column(Float, default=0.0)

    job = relationship("Job", back_populates="candidates")


class Match(Base):
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True)
    job_id = Column(String(16), ForeignKey("jobs.id"), index=True)
    tmdb_id = Column(Integer)
    title = Column(String(400))
    year = Column(Integer)
    poster_path = Column(String(300))
    overview = Column(Text, default="")
    state = Column(String(20), default=REVIEW)
    alternates_json = Column(Text, default="[]")

    job = relationship("Job", back_populates="matches")

    @property
    def alternates(self):
        try:
            return json.loads(self.alternates_json or "[]")
        except json.JSONDecodeError:
            return []


class Publish(Base):
    __tablename__ = "publishes"

    id = Column(Integer, primary_key=True)
    job_id = Column(String(16), ForeignKey("jobs.id"), index=True)
    target = Column(String(20))  # tmdb | radarr | kometa
    status = Column(String(20))  # ok | error
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    job = relationship("Job", back_populates="publishes")


class KeyValue(Base):
    """Small persistent store (e.g. cached TMDB session id)."""
    __tablename__ = "kv"

    key = Column(String(100), primary_key=True)
    value = Column(Text, default="")


engine = create_engine(
    config.DATABASE_URL, connect_args={"check_same_thread": False}, future=True
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db():
    Base.metadata.create_all(engine)
    _migrate()


def _migrate():
    """Add newly-introduced columns to existing tables without dropping data."""
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(jobs)")}
        if "radarr_status" not in cols:
            conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN radarr_status TEXT DEFAULT ''")
        if "radarr_checked_at" not in cols:
            conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN radarr_checked_at DATETIME")
        if "content_type" not in cols:
            conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN content_type TEXT DEFAULT 'auto'")


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def kv_get(session, key, default=None):
    row = session.get(KeyValue, key)
    return row.value if row else default


def kv_set(session, key, value):
    row = session.get(KeyValue, key)
    if row:
        row.value = value
    else:
        session.add(KeyValue(key=key, value=value))
