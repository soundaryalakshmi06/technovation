"""
AthleteGuard - database foundation (PostgreSQL + SQLAlchemy).

Keeps the names the existing backend already imports:
    from database import get_db, SessionLocal, Base

Additions:
    engine, session_scope(), check_connection(), init_db()

init_db() creates any missing tables and then applies SCHEMA_PATCHES, a list of
idempotent "ADD COLUMN IF NOT EXISTS" / "CREATE INDEX IF NOT EXISTS" statements,
so an EXISTING PostgreSQL database (created by the old models.py) is upgraded in
place. Nothing is ever dropped or deleted.
"""

import logging
from contextlib import contextmanager
from typing import Iterator, List

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from config import ensure_directories, settings

logger = logging.getLogger("athleteguard.database")


# ============================================
# ENGINE / SESSION / BASE
# ============================================

engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    pool_pre_ping=True,                       # drops dead connections instead of failing the request
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
    pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Deterministic constraint / index names for every NEW table.
# (Existing tables keep whatever names PostgreSQL already gave them.)
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Base = declarative_base(metadata=MetaData(naming_convention=NAMING_CONVENTION))


# ============================================
# SESSION HELPERS
# ============================================

def get_db() -> Iterator:
    """FastAPI dependency (unchanged behaviour)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator:
    """
    For scripts and background jobs (outside a request):

        with session_scope() as db:
            db.add(...)

    Commits on success, rolls back on error, always closes.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_connection() -> bool:
    """True when PostgreSQL answers (used by the future /system/status endpoint)."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception as error:
        logger.warning("Database connection check failed: %s", error)
        return False


# ============================================
# IN-PLACE UPGRADE OF AN EXISTING DATABASE
# Mirrors every column / index that models.py adds to a table that already
# existed in the old schema. Brand-new tables are created by create_all().
# Every statement is safe to run repeatedly.
# ============================================

SCHEMA_PATCHES: List[str] = [

    # ---- teams ----
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS short_name VARCHAR(20)",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",

    # ---- users ----
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(120)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(32)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)",

    # ---- players ----
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS position VARCHAR(50)",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS reference_images JSON",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS face_embedding JSON",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "CREATE INDEX IF NOT EXISTS ix_players_team_jersey ON players (team_id, jersey_number)",

    # ---- match_sessions ----
    "ALTER TABLE match_sessions ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'CREATED'",
    "ALTER TABLE match_sessions ADD COLUMN IF NOT EXISTS created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
    "ALTER TABLE match_sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE match_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",

    # ---- injury_events (fall / injury records) ----
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS player_track_id INTEGER REFERENCES player_tracks(id) ON DELETE SET NULL",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS collision_event_id INTEGER REFERENCES collision_events(id) ON DELETE SET NULL",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS side VARCHAR(10)",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS risk_score DOUBLE PRECISION",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS frame_number INTEGER",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS location_x DOUBLE PRECISION",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS location_y DOUBLE PRECISION",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(255)",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS detection_count INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS resolved_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "CREATE INDEX IF NOT EXISTS ix_injury_events_player ON injury_events (player_id)",
    "CREATE INDEX IF NOT EXISTS ix_injury_events_match_track ON injury_events (match_session_id, track_id)",
    "CREATE INDEX IF NOT EXISTS ix_injury_events_match_time ON injury_events (match_session_id, video_time_sec)",
    "CREATE INDEX IF NOT EXISTS ix_injury_events_collision ON injury_events (collision_event_id)",
    'CREATE INDEX IF NOT EXISTS ix_injury_events_timestamp ON injury_events ("timestamp")',
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_injury_match_dedupe ON injury_events (match_session_id, dedupe_key)",
]


def apply_schema_patches() -> List[str]:
    """
    Runs SCHEMA_PATCHES one statement per transaction, so a single failure
    never blocks the others. Returns the statements that failed (normally []).
    """
    failed: List[str] = []

    for statement in SCHEMA_PATCHES:
        try:
            with engine.begin() as connection:
                connection.execute(text(statement))
        except Exception as error:
            failed.append(statement)
            logger.warning("Schema patch skipped: %s | %s", statement[:80], error)

    return failed


def init_db(apply_patches: bool = True) -> List[str]:
    """
    1. creates the output / clips / videos folders
    2. creates every missing table (new databases get the full schema)
    3. upgrades tables that already exist (existing databases)

    Call once at application startup. Returns the failed patch statements.
    """
    ensure_directories()

    # Importing models registers every table on Base.metadata.
    # Done here (not at module level) to avoid a circular import.
    import models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    if apply_patches:
        return apply_schema_patches()

    return []
