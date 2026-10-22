"""Environment-backed configuration."""
import os


def _clean(value: str | None) -> str:
    return (value or "").strip()


LLM_BASE_URL = _clean(os.getenv("LLM_BASE_URL", "http://192.168.0.237:4141/v1"))
LLM_API_KEY = _clean(os.getenv("LLM_API_KEY", "dummy"))
LLM_MODEL = _clean(os.getenv("LLM_MODEL", "gemini-3.8-flash"))

TMDB_API_KEY = _clean(os.getenv("TMDB_API_KEY"))
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"

WHISPER_ASR_URL = _clean(os.getenv("WHISPER_ASR_URL", "http://192.168.0.237:9000")).rstrip("/")

# Netscape cookies file for yt-dlp (Instagram etc. require login). Optional.
YTDLP_COOKIES = _clean(os.getenv("YTDLP_COOKIES", "/data/cookies.txt"))

RADARR_URL = _clean(os.getenv("RADARR_URL", "http://192.168.0.237:7878")).rstrip("/")
RADARR_WEB_URL = _clean(os.getenv("RADARR_WEB_URL", "https://radarr.tokyo3.eu")).rstrip("/")
RADARR_API_KEY = _clean(os.getenv("RADARR_API_KEY"))
RADARR_QUALITY_PROFILE = _clean(os.getenv("RADARR_QUALITY_PROFILE", "2160p Balanced"))
RADARR_ROOT_FOLDER = _clean(os.getenv("RADARR_ROOT_FOLDER", "/media/Movies"))

KOMETA_CONFIG_DIR = _clean(os.getenv("KOMETA_CONFIG_DIR", "/kometa-config"))
KOMETA_LIBRARY = _clean(os.getenv("KOMETA_LIBRARY", "Films"))
KOMETA_CONTAINER = _clean(os.getenv("KOMETA_CONTAINER", "kometa"))

DATA_DIR = _clean(os.getenv("DATA_DIR", "/data"))
DATABASE_URL = _clean(os.getenv("DATABASE_URL")) or f"sqlite:///{os.path.join(DATA_DIR, 'collecta.db')}"

# How often the scheduler checks for due scheduled parsings.
SCHEDULER_TICK_SECONDS = int(_clean(os.getenv("SCHEDULER_TICK_SECONDS")) or 60)

JOBS_DIR = os.path.join(DATA_DIR, "jobs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)
