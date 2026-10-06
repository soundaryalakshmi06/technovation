"""
AthleteGuard - central backend configuration.

Dependency order of the Batch 1 files (no circular imports):

    config.py  ->  database.py  ->  models.py  ->  schemas.py
    (schemas.py only reads constants from config.py; it never imports models)

Everything can be overridden with environment variables or a .env file
(backend/.env or <project root>/.env). No extra packages are needed.
The defaults match the values already used by the existing backend, so the
project keeps working without any configuration.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


# ============================================
# PATHS
# ============================================

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)


# ============================================
# .env LOADER (tiny, dependency-free)
# Real environment variables always win over the file.
# ============================================

def _load_env_file(path: str) -> None:

    if not os.path.isfile(path):
        return

    with open(path, "r", encoding="utf-8-sig") as handle:
        for raw_line in handle:

            line = raw_line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key and key not in os.environ:
                os.environ[key] = value


_load_env_file(os.path.join(BACKEND_DIR, ".env"))
_load_env_file(os.path.join(PROJECT_ROOT, ".env"))


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: List[str]) -> List[str]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _normalize_database_url(url: str) -> str:
    # Some hosts hand out "postgres://", which SQLAlchemy 1.4+ no longer accepts.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


# ============================================
# SHARED VOCABULARY
# Stored as plain strings in the database (this is how the existing
# backend already stores them), validated in schemas.py.
# ============================================

# Roles (same three roles as the existing backend)
ROLE_ADMIN = "admin"
ROLE_COACH = "coach"
ROLE_MEDICAL = "medical"
ROLE_PLAYER = "player"
ROLES: Tuple[str, ...] = (ROLE_ADMIN, ROLE_COACH, ROLE_MEDICAL, ROLE_PLAYER)

# Event types written by the AI pipeline. Manual events may use any text,
# so the database does not restrict event_type.
EVENT_TYPE_FALL = "FALL"
EVENT_TYPE_COLLISION = "COLLISION"

# Risk levels used by the screening pipeline and dashboard.
RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"
RISK_CRITICAL = "CRITICAL"
RISK_LEVELS: Tuple[str, ...] = (RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL)

# Where an event came from
SOURCE_AI = "ai"
SOURCE_MANUAL = "manual"
EVENT_SOURCES: Tuple[str, ...] = (SOURCE_AI, SOURCE_MANUAL)

# How a player was recognised (matches alert_system.METHOD_LABELS)
IDENTIFIED_BY_METHODS: Tuple[str, ...] = (
    "jersey",
    "jersey_name",
    "jersey_history",
    "jersey_handoff",
    "face",
    "manual",
)

# Identity state of a tracker id (spec section 11)
IDENTITY_CONFIRMED = "CONFIRMED"
IDENTITY_UNCERTAIN = "UNCERTAIN"
IDENTITY_UNKNOWN = "UNKNOWN"
IDENTITY_STATUSES: Tuple[str, ...] = (IDENTITY_CONFIRMED, IDENTITY_UNCERTAIN, IDENTITY_UNKNOWN)

# Match / video lifecycle
MATCH_STATUSES: Tuple[str, ...] = ("CREATED", "VIDEO_UPLOADED", "PROCESSING", "COMPLETED", "FAILED")
VIDEO_SOURCE_TYPES: Tuple[str, ...] = ("file", "webcam", "capture_card", "stream")
VIDEO_STATUSES: Tuple[str, ...] = ("UPLOADED", "PROCESSING", "PROCESSED", "FAILED")

# Alerts
ALERT_TYPES: Tuple[str, ...] = ("NEW_EVENT", "PLAYER_IDENTIFIED", "ESCALATION", "SYSTEM")
ALERT_CHANNELS: Tuple[str, ...] = ("web", "whatsapp")
ALERT_STATUSES: Tuple[str, ...] = ("PENDING", "SENT", "FAILED", "SKIPPED")

# A collision always has exactly two participants: slot 1 and slot 2
COLLISION_PARTICIPANT_SLOTS: Tuple[int, ...] = (1, 2)


# ============================================
# SETTINGS
# ============================================

@dataclass(frozen=True)
class Settings:

    APP_NAME: str = "AthleteGuard"
    APP_VERSION: str = "1.0.0"

    # ---- PostgreSQL (default = the URL the existing backend already uses) ----
    DATABASE_URL: str = "postgresql://injury_app_user:injury123@localhost:5432/injury_detection_db"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT_SECONDS: int = 30
    DB_POOL_RECYCLE_SECONDS: int = 1800

    # ---- JWT (same defaults as auth.py; set SECRET_KEY in .env for real use) ----
    SECRET_KEY: str = "change_this_to_something_random_and_long"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    # ---- HTTP ----
    CORS_ORIGINS: List[str] = field(default_factory=lambda: [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
    ])
    API_BASE_URL: str = "http://127.0.0.1:8000"
    REQUEST_TIMEOUT_SECONDS: int = 3

    # ---- AI ingest authentication (service token for the AI pipeline) ----
    # The AI process authenticates to the write/ingest endpoints with the X-Service-Token
    # header. Both values are read from the environment / .env - never hardcode them in source.
    AI_INGEST_REQUIRE_AUTH: bool = True
    AI_SERVICE_TOKEN: str = ""

    # ---- Folders ----
    OUTPUT_DIR: str = os.path.join(PROJECT_ROOT, "output")
    CLIPS_DIR: str = os.path.join(PROJECT_ROOT, "output", "clips")
    VIDEOS_DIR: str = os.path.join(PROJECT_ROOT, "videos")

    # ---- Video uploads ----
    MAX_VIDEO_UPLOAD_MB: int = 2048
    ALLOWED_VIDEO_EXTENSIONS: Tuple[str, ...] = (".mp4", ".avi", ".mov", ".mkv", ".webm")

    # ---- Event de-duplication (same defaults as multi_player_injury.py) ----
    FALL_COOLDOWN_SECONDS: float = 5.0          # same player/track, same kind of event
    COLLISION_COOLDOWN_SECONDS: float = 5.0     # same pair of tracks
    INCIDENT_MERGE_SECONDS: float = 3.0         # detections this close in time ...
    INCIDENT_MERGE_DISTANCE: float = 1.2        # ... and in space (x box size) are one incident
    DEDUPE_BUCKET_SECONDS: float = 3.0          # time bucket used inside dedupe_key strings

    # ---- Identity confidence thresholds (spec section 11) ----
    IDENTITY_CONFIRMED_MIN_CONFIDENCE: float = 0.75
    IDENTITY_UNCERTAIN_MIN_CONFIDENCE: float = 0.40

    # ---- Alerts ----
    WHATSAPP_EVENT_TYPES: Tuple[str, ...] = (EVENT_TYPE_FALL,)
    HIGH_RISK_ALERT_CHANNELS: Tuple[str, ...] = ("web", "whatsapp")

    def identity_status_for(self, confidence) -> str:
        """Maps an identity confidence (0..1) to CONFIRMED / UNCERTAIN / UNKNOWN."""
        if confidence is None:
            return IDENTITY_UNKNOWN
        if confidence >= self.IDENTITY_CONFIRMED_MIN_CONFIDENCE:
            return IDENTITY_CONFIRMED
        if confidence >= self.IDENTITY_UNCERTAIN_MIN_CONFIDENCE:
            return IDENTITY_UNCERTAIN
        return IDENTITY_UNKNOWN


def load_settings() -> Settings:

    return Settings(
        DATABASE_URL=_normalize_database_url(_env_str("DATABASE_URL", Settings.DATABASE_URL)),
        DB_ECHO=_env_bool("DB_ECHO", Settings.DB_ECHO),
        DB_POOL_SIZE=_env_int("DB_POOL_SIZE", Settings.DB_POOL_SIZE),
        DB_MAX_OVERFLOW=_env_int("DB_MAX_OVERFLOW", Settings.DB_MAX_OVERFLOW),
        DB_POOL_TIMEOUT_SECONDS=_env_int("DB_POOL_TIMEOUT_SECONDS", Settings.DB_POOL_TIMEOUT_SECONDS),
        DB_POOL_RECYCLE_SECONDS=_env_int("DB_POOL_RECYCLE_SECONDS", Settings.DB_POOL_RECYCLE_SECONDS),
        SECRET_KEY=_env_str("SECRET_KEY", Settings.SECRET_KEY),
        JWT_ALGORITHM=_env_str("JWT_ALGORITHM", Settings.JWT_ALGORITHM),
        ACCESS_TOKEN_EXPIRE_MINUTES=_env_int("ACCESS_TOKEN_EXPIRE_MINUTES", Settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        CORS_ORIGINS=_env_list("CORS_ORIGINS", [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:5174",
            "http://localhost:5174",
        ]),
        API_BASE_URL=_env_str("API_BASE_URL", Settings.API_BASE_URL),
        REQUEST_TIMEOUT_SECONDS=_env_int("REQUEST_TIMEOUT_SECONDS", Settings.REQUEST_TIMEOUT_SECONDS),
        AI_INGEST_REQUIRE_AUTH=_env_bool("AI_INGEST_REQUIRE_AUTH", Settings.AI_INGEST_REQUIRE_AUTH),
        AI_SERVICE_TOKEN=_env_str("AI_SERVICE_TOKEN", Settings.AI_SERVICE_TOKEN),
        OUTPUT_DIR=_env_str("OUTPUT_DIR", Settings.OUTPUT_DIR),
        CLIPS_DIR=_env_str("CLIPS_DIR", Settings.CLIPS_DIR),
        VIDEOS_DIR=_env_str("VIDEOS_DIR", Settings.VIDEOS_DIR),
        MAX_VIDEO_UPLOAD_MB=_env_int("MAX_VIDEO_UPLOAD_MB", Settings.MAX_VIDEO_UPLOAD_MB),
        FALL_COOLDOWN_SECONDS=_env_float("FALL_COOLDOWN_SECONDS", Settings.FALL_COOLDOWN_SECONDS),
        COLLISION_COOLDOWN_SECONDS=_env_float("COLLISION_COOLDOWN_SECONDS", Settings.COLLISION_COOLDOWN_SECONDS),
        INCIDENT_MERGE_SECONDS=_env_float("INCIDENT_MERGE_SECONDS", Settings.INCIDENT_MERGE_SECONDS),
        INCIDENT_MERGE_DISTANCE=_env_float("INCIDENT_MERGE_DISTANCE", Settings.INCIDENT_MERGE_DISTANCE),
        DEDUPE_BUCKET_SECONDS=_env_float("DEDUPE_BUCKET_SECONDS", Settings.DEDUPE_BUCKET_SECONDS),
        IDENTITY_CONFIRMED_MIN_CONFIDENCE=_env_float(
            "IDENTITY_CONFIRMED_MIN_CONFIDENCE", Settings.IDENTITY_CONFIRMED_MIN_CONFIDENCE
        ),
        IDENTITY_UNCERTAIN_MIN_CONFIDENCE=_env_float(
            "IDENTITY_UNCERTAIN_MIN_CONFIDENCE", Settings.IDENTITY_UNCERTAIN_MIN_CONFIDENCE
        ),
    )


settings = load_settings()


def ensure_directories() -> Dict[str, str]:
    """Creates the output / clips / videos folders if they are missing."""

    created = {}

    for name in ("OUTPUT_DIR", "CLIPS_DIR", "VIDEOS_DIR"):
        path = getattr(settings, name)
        os.makedirs(path, exist_ok=True)
        created[name] = path

    return created
