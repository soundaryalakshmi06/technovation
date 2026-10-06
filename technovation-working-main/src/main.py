"""
AthleteGuard / Technovation - FastAPI application (PostgreSQL).

Run (from the src/ folder, which is the import root - same as the AI scripts):
    uvicorn main:app --reload --port 8000

Structure
  Routes are grouped into routers below (auth, users, matches, events, collisions, tracks, players,
  teams, videos, alerts, media). Batch 6 can move each router into its own module without changing a
  single URL.

Identity rule applied by every endpoint that touches players
  track_id  = temporary tracker id (one run of the tracker).     NOT a database id.
  player_id = permanent players.id.
  An API caller can attach a player_id only as a CONFIRMED identity; UNKNOWN / UNCERTAIN stay
  "Unknown Player". A track never turns into a player because the two numbers happen to match.

Backward compatibility (the existing dashboard and AI scripts keep working unchanged)
  Same paths, same request bodies, same response keys for: /auth/login, /matches, /events,
  /events/{id}/ai-update, /events/{id}/assign, /events/manual, /events/{id}/resolve, /players, /teams,
  /admin/players/clear, /clips/...  New keys are only ever ADDED.
  One deliberate change: write endpoints the AI script uses (POST /matches, POST /events, PATCH ai-update)
  now require authentication - see dependencies.require_service_or_roles.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import logging.handlers
import mimetypes
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import and_, case, exists, func, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, joinedload, selectinload
from starlette.concurrency import run_in_threadpool

from auth import change_password, create_user, login_user, update_user
from injury_notes import build_medical_assessment
import config as _config

# main.py was also used with a later configuration vocabulary. Keep those names local
# to the application module so the Batch 1 config and its persisted values stay intact.
settings = _config.settings
ALERT_STATUS_FAILED, ALERT_STATUS_PENDING = "FAILED", "PENDING"
ALERT_STATUS_SKIPPED, ALERT_STATUS_SENT = "SKIPPED", "SENT"
COLLISION_ACTIVE, COLLISION_DETECTED, COLLISION_ENDED = "ACTIVE", "DETECTED", "ENDED"
EVENT_SOURCE_AI, EVENT_SOURCE_MANUAL = _config.SOURCE_AI, _config.SOURCE_MANUAL
EVENT_TYPE_FALL = _config.EVENT_TYPE_FALL
IDENTITY_CONFIRMED = _config.IDENTITY_CONFIRMED
IDENTITY_UNCERTAIN = _config.IDENTITY_UNCERTAIN
IDENTITY_UNKNOWN = _config.IDENTITY_UNKNOWN
MATCH_LIVE, MATCH_FINISHED = "PROCESSING", "COMPLETED"
RISK_HIGH = _config.RISK_HIGH
RISK_CRITICAL = _config.RISK_CRITICAL
RISK_ORDER = {
    _config.RISK_LOW: 1,
    _config.RISK_MEDIUM: 2,
    _config.RISK_HIGH: 3,
    _config.RISK_CRITICAL: 4,
}
ROLE_ADMIN, ROLE_COACH, ROLE_MEDICAL, ROLE_PLAYER = (
    _config.ROLE_ADMIN, _config.ROLE_COACH, _config.ROLE_MEDICAL, _config.ROLE_PLAYER)
STAFF_ROLES = (ROLE_ADMIN, ROLE_MEDICAL)
VIDEO_STATUSES = _config.VIDEO_STATUSES
VIDEO_UPLOADED = "UPLOADED"

# dependencies.py imports STAFF_ROLES and reads these settings at request time.
# Add compatibility defaults to this instance only; config.py remains untouched.
_compat_settings = {
    "AI_INGEST_REQUIRE_AUTH": True, "AI_SERVICE_TOKEN": "", "ALLOW_PUBLIC_REGISTRATION": False,
    "ALLOW_UNIDENTIFIED_EVENTS": True, "CLIPS_REQUIRE_SIGNATURE": True,
    "CLIP_URL_TTL_SECONDS": 300, "COLLISION_DEDUP_WINDOW_SEC": 5.0,
    "COLLISION_TO_INJURY_WINDOW_SEC": 8.0, "CORS_ALLOW_CREDENTIALS": True,
    "ENVIRONMENT": "development", "FALL_DEDUP_WINDOW_SEC": 5.0,
    "IDENTITY_CONFIRM_MIN_CONFIDENCE": settings.IDENTITY_CONFIRMED_MIN_CONFIDENCE,
    "JWT_SECRET": settings.SECRET_KEY, "LOG_FORMAT": "text", "LOG_LEVEL": "INFO",
    "LOG_DIR": __import__("pathlib").Path(_config.PROJECT_ROOT) / "logs",
}
for _setting_name, _setting_value in _compat_settings.items():
    if not hasattr(settings, _setting_name):
        object.__setattr__(settings, _setting_name, _setting_value)
if not hasattr(settings, "is_production"):
    object.__setattr__(settings, "is_production", False)
if not hasattr(settings, "safe_summary"):
    object.__setattr__(settings, "safe_summary", lambda: f"{settings.APP_NAME} {settings.APP_VERSION}")
_config.STAFF_ROLES = STAFF_ROLES

from config import ensure_directories
from database import check_connection, engine, get_db
from dependencies import (
    Principal, accessible_team_ids, ensure_team_access, get_current_active_user,
    get_optional_user, require_admin, require_roles, require_service_or_roles, require_staff,
)
from models import (
    Alert, CollisionEvent, CollisionParticipant, InjuryEvent, MatchSession, Player, PlayerTrack, Team, User,
    Video,
)
from schemas import (
    AlertCreate, AlertMarkRead, AlertRead, AlertStatusUpdate,
    AssignPlayerRequest, CollisionCreateResponse, CollisionEventCreate, CollisionEventRead,
    CollisionEventUpdate, CollisionParticipantAssign, EventAIUpdateRequest, EventCreateRequest,
    EventCreateResponse, InjuryEventRead, LoginRequest, ManualEventRequest,
    MatchCreateRequest, MatchRead, MatchUpdate, MedicalAssessmentRead, MessageResponse,
    PasswordChangeRequest, PlayerBrief, PlayerCreate, PlayerRead, PlayerStatsRead,
    PlayerTrackRead, PlayerTrackUpsert, PlayerUpdate, TeamCreate, TeamRead,
    TeamSummaryRead, TeamUpdate, TokenResponse, UserCreate, UserRead, UserUpdate, VideoCreate, VideoRead,
    VideoUpdate,
)

from pathlib import Path
from pydantic import BaseModel


class HealthRead(BaseModel):
    status: str
    database: str
    version: str
    environment: str


class TeamPlayerSummary(BaseModel):
    id: int
    name: str
    jersey_number: int
    injury_events: int = 0
    high_risk_events: int = 0

logger = logging.getLogger("athleteguard.api")


# ===========================================================================
# Logging
# ===========================================================================
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_athleteguard_configured", False):
        return
    formatter: logging.Formatter = (
        _JsonFormatter() if settings.LOG_FORMAT == "json"
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    try:
        Path(settings.LOG_DIR).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            Path(settings.LOG_DIR) / "backend.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass  # file logging is optional; console logging always works
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL)
    root._athleteguard_configured = True  # type: ignore[attr-defined]


# ===========================================================================
# Database readiness (safe startup + throttled retry)
# ===========================================================================
_DB_RETRY_SECONDS = 5.0
_db_lock = threading.Lock()
_db_last_attempt = 0.0


def _initialise_database(app: FastAPI) -> bool:
    """
    Check database connectivity only. Schema setup and bootstrap-user creation are external tasks.
    """
    global _db_last_attempt
    with _db_lock:
        if getattr(app.state, "db_ready", False):
            return True
        _db_last_attempt = time.monotonic()
        if not check_connection():
            logger.error("PostgreSQL is not reachable - API routes answer 503 until it is")
            return False
        app.state.db_ready = True
        logger.info("database connection ready; no schema or account changes were made")
        return True


# ===========================================================================
# Application lifecycle
# ===========================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    ensure_directories()
    logger.info("starting %s", settings.safe_summary())
    app.state.db_ready = False
    _initialise_database(app)
    if not settings.AI_SERVICE_TOKEN and settings.AI_INGEST_REQUIRE_AUTH:
        logger.warning("AI_SERVICE_TOKEN is not set: the AI pipeline can only call write endpoints with an "
                       "admin bearer token. Set AI_SERVICE_TOKEN in .env and give the same value to the AI process.")
    yield
    engine.dispose()
    logger.info("shutdown complete")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)


# ===========================================================================
# Middleware
# NOTE: Starlette makes the LAST added middleware the OUTERMOST one. The database gate is added first
# and CORS second, so CORS wraps everything and even the 503 answers carry CORS headers.
# ===========================================================================
_DB_EXEMPT_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json"}
_DB_EXEMPT_PREFIXES = ("/clips/",)   # clip delivery needs no database access


@app.middleware("http")
async def database_gate(request: Request, call_next):
    if (request.method == "OPTIONS" or request.url.path in _DB_EXEMPT_PATHS
            or request.url.path.startswith(_DB_EXEMPT_PREFIXES)):
        return await call_next(request)
    if not getattr(request.app.state, "db_ready", False):
        if time.monotonic() - _db_last_attempt >= _DB_RETRY_SECONDS:
            await run_in_threadpool(_initialise_database, request.app)
        if not getattr(request.app.state, "db_ready", False):
            return JSONResponse(status_code=503, content={"detail": "Database not ready, please retry shortly"},
                                headers={"Retry-After": str(int(_DB_RETRY_SECONDS))})
    return await call_next(request)


_DEV_CORS_ORIGINS = [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5173", "http://127.0.0.1:5173", "http://127.0.0.1:5174",
    "http://localhost:5174", "http://localhost:8080", "http://127.0.0.1:8080",
]
_CORS_ORIGINS: List[str] = list(settings.CORS_ORIGINS)
if not _CORS_ORIGINS and not settings.is_production:
    _CORS_ORIGINS = list(_DEV_CORS_ORIGINS)
# Browsers reject "Access-Control-Allow-Origin: *" together with credentials.
_CORS_CREDENTIALS: bool = bool(settings.CORS_ALLOW_CREDENTIALS) and "*" not in _CORS_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=_CORS_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _cors_headers(request: Request) -> Dict[str, str]:
    """CORS headers for responses produced OUTSIDE the CORS middleware (the 500 handler)."""
    origin = request.headers.get("origin")
    if not origin:
        return {}
    if "*" in _CORS_ORIGINS:
        return {"Access-Control-Allow-Origin": "*"}
    if origin in _CORS_ORIGINS:
        headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        if _CORS_CREDENTIALS:
            headers["Access-Control-Allow-Credentials"] = "true"
        return headers
    return {}


# ===========================================================================
# Error handling - consistent {"detail": ...}, never leaks internals
# ===========================================================================
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default body echoes the submitted input (including passwords) - strip it.
    errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "invalid"), "type": e.get("type", "")}
              for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    logger.warning("integrity error on %s %s: %s", request.method, request.url.path, str(exc.orig)[:300])
    return JSONResponse(status_code=409, content={"detail": "The request conflicts with existing data"})


@app.exception_handler(OperationalError)
async def database_unavailable_handler(request: Request, exc: OperationalError) -> JSONResponse:
    logger.error("database unavailable on %s %s: %s", request.method, request.url.path, type(exc.orig).__name__)
    return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    error_id = uuid.uuid4().hex[:12]
    logger.exception("unhandled error %s on %s %s", error_id, request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "error_id": error_id},
                        headers=_cors_headers(request))


# ===========================================================================
# Shared helpers
# ===========================================================================
def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- signed clip URLs: <video src> cannot send an Authorization header --------------------------
def _media_key() -> bytes:
    return hmac.new(settings.JWT_SECRET.encode("utf-8"), b"athleteguard-media-v1", hashlib.sha256).digest()


def _normalise_clip_path(clip_path: str) -> str:
    return clip_path.replace("\\", "/").lstrip("/")


def _clip_signature(clip_path: str, expires: int) -> str:
    message = f"{_normalise_clip_path(clip_path)}|{expires}".encode("utf-8")
    return hmac.new(_media_key(), message, hashlib.sha256).hexdigest()


def build_clip_url(clip_path: str) -> str:
    """Legacy '/clips/<file>' shape, with a short-lived signature unless CLIPS_REQUIRE_SIGNATURE=false."""
    normalised = _normalise_clip_path(clip_path)
    safe = quote(normalised, safe="/")
    if not settings.CLIPS_REQUIRE_SIGNATURE:
        return "/clips/" + safe
    expires = int(time.time()) + settings.CLIP_URL_TTL_SECONDS
    return f"/clips/{safe}?exp={expires}&sig={_clip_signature(normalised, expires)}"


def _event_read(event: InjuryEvent) -> InjuryEventRead:
    result = InjuryEventRead.from_event(event)
    return result.model_copy(update={"clip_url": build_clip_url(event.clip_path)} if event.clip_path else {})


def _collision_read(collision: CollisionEvent, viewer: Optional[User] = None) -> CollisionEventRead:
    result = CollisionEventRead.model_validate(collision)
    if viewer is not None and viewer.role == ROLE_PLAYER:
        participants = [
            part if part.player_id == viewer.player_id else part.model_copy(update={
                "player_id": None,
                "player": None,
                "identified_by": None,
                "id_detail": None,
                "id_confidence": None,
                "identified_at": None,
            })
            for part in result.participants
        ]
        result = result.model_copy(update={"participants": participants, "player_ids": [viewer.player_id]})
    return result.model_copy(update={"clip_url": build_clip_url(collision.clip_path)} if collision.clip_path else {})


# ---- lookups ------------------------------------------------------------------------------------
def _match_or_404(db: Session, match_id: int) -> MatchSession:
    match = db.query(MatchSession).filter(MatchSession.id == match_id).first()
    if match is None:
        raise HTTPException(status_code=404, detail="Match session not found")
    return match


def _scope_matches_to_user(query, user: User):
    if user.role in STAFF_ROLES:
        return query
    if user.role not in (ROLE_COACH, ROLE_PLAYER):
        raise HTTPException(status_code=403, detail="You do not have permission to access matches")
    if user.team_id is None:
        return query.filter(MatchSession.id.in_([]))
    return query.filter(or_(MatchSession.team_a_id == user.team_id, MatchSession.team_b_id == user.team_id))


def _ensure_match_access(match: MatchSession, user: User) -> None:
    if user.role in STAFF_ROLES:
        return
    if user.role in (ROLE_COACH, ROLE_PLAYER) and user.team_id is not None:
        if user.team_id in (match.team_a_id, match.team_b_id):
            return
    raise HTTPException(status_code=403, detail="You can only access matches involving your own team")


def _player_profile_for_user(db: Session, user: User) -> Player:
    if user.role != ROLE_PLAYER or user.player_id is None:
        raise HTTPException(status_code=403, detail="This account has no player profile")
    player = db.query(Player).filter(Player.id == user.player_id, Player.active.is_(True)).first()
    if player is None:
        raise HTTPException(status_code=403, detail="Player profile is unavailable")
    return player


def _ensure_player_record_access(user: User, player: Player) -> None:
    if user.role == ROLE_PLAYER:
        if user.player_id != player.id:
            raise HTTPException(status_code=403, detail="You can only access your own player profile")
        return
    ensure_team_access(user, player.team_id)


def _player_or_404(db: Session, player_id: int) -> Player:
    player = (db.query(Player).options(joinedload(Player.team)).filter(Player.id == player_id).first())
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")
    return player


def _event_query(db: Session):
    return db.query(InjuryEvent).options(
        joinedload(InjuryEvent.player).joinedload(Player.team), joinedload(InjuryEvent.match))


def _event_or_404(db: Session, event_id: int) -> InjuryEvent:
    event = _event_query(db).filter(InjuryEvent.id == event_id).first()
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


def _collision_query(db: Session):
    return db.query(CollisionEvent).options(
        selectinload(CollisionEvent.participants).joinedload(CollisionParticipant.player).joinedload(Player.team),
        selectinload(CollisionEvent.injury_events),
        joinedload(CollisionEvent.match),
    )


def _collision_or_404(db: Session, collision_id: int) -> CollisionEvent:
    collision = _collision_query(db).filter(CollisionEvent.id == collision_id).first()
    if collision is None:
        raise HTTPException(status_code=404, detail="Collision event not found")
    return collision


# ---- identity / roster safety ----------------------------------------------------------------------
def _ensure_player_usable(player: Player, source: str) -> None:
    """The AI may only attach players that are in the active analysis roster."""
    if source == EVENT_SOURCE_AI and not player.active:
        raise HTTPException(status_code=409, detail=f"Player '{player.name}' is not in the active roster")


def _ensure_player_in_match(match: Optional[MatchSession], player: Player) -> None:
    """When a match has its two teams set, a player from any other team is a data error, not a guess."""
    if match is None:
        return
    teams = {t for t in (match.team_a_id, match.team_b_id) if t is not None}
    if teams and player.team_id not in teams:
        raise HTTPException(status_code=400, detail=f"Player '{player.name}' does not belong to this match's teams")


def _validate_teams(db: Session, team_a_id: Optional[int], team_b_id: Optional[int]) -> None:
    if team_a_id is not None and team_b_id is not None and team_a_id == team_b_id:
        raise HTTPException(status_code=400, detail="team_a_id and team_b_id must be two different teams")
    for team_id in (team_a_id, team_b_id):
        if team_id is not None and db.query(Team.id).filter(Team.id == team_id).first() is None:
            raise HTTPException(status_code=404, detail=f"Team {team_id} not found")


def _validate_player_track(db: Session, player_track_id: Optional[int], player: Optional[Player],
                           match_id: Optional[int]) -> None:
    if player_track_id is None:
        return
    track = db.query(PlayerTrack).filter(PlayerTrack.id == player_track_id).first()
    if track is None:
        raise HTTPException(status_code=404, detail="Player track not found")
    if match_id is not None and track.match_session_id != match_id:
        raise HTTPException(status_code=400, detail="Player track belongs to a different match")
    if player is not None and track.player_id not in (None, player.id):
        raise HTTPException(status_code=409, detail="Player track is linked to a different player")
    if player is None and track.player_id is not None:
        raise HTTPException(status_code=409, detail="An unidentified event cannot link to a player-identified track")


def _validate_video(db: Session, video_id: Optional[int], match_id: Optional[int]) -> None:
    if video_id is None:
        return
    video = db.query(Video).filter(Video.id == video_id).first()
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if match_id is not None and video.match_session_id != match_id:
        raise HTTPException(status_code=400, detail="Video belongs to a different match")


def _effective_status(player: Optional[Player], requested: Optional[str], confidence: Optional[float],
                      identified_by: Optional[str]) -> str:
    """
    identity_status to store. No player -> UNKNOWN (or UNCERTAIN if the caller said so).
    A player attached with a confidence below the configured threshold is stored as UNCERTAIN so the
    dashboard can tell it apart; manual assignment is always CONFIRMED.
    """
    if player is None:
        return IDENTITY_UNCERTAIN if requested == IDENTITY_UNCERTAIN else IDENTITY_UNKNOWN
    if requested:
        return requested
    if identified_by != "manual" and confidence is not None and confidence < settings.IDENTITY_CONFIRM_MIN_CONFIDENCE:
        logger.warning("player %s attached with low id_confidence=%.2f (< %.2f) -> stored as UNCERTAIN",
                       player.id, confidence, settings.IDENTITY_CONFIRM_MIN_CONFIDENCE)
        return IDENTITY_UNCERTAIN
    return IDENTITY_CONFIRMED


def _escalate(current: Optional[str], new: Optional[str]) -> Optional[str]:
    """Risk only ever goes up when the same physical event is re-reported."""
    if new is None:
        return current
    if current is None or RISK_ORDER.get(new, 0) > RISK_ORDER.get(current, 0):
        return new
    return current


def _max(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


# ===========================================================================
# Collision <-> fall association helpers
# ===========================================================================
def _participant_matches(collision: CollisionEvent, player_id: Optional[int], track_id: Optional[int],
                         video_id: Optional[int]) -> bool:
    """Was this player (or, if still unknown, this very track) one of the two collision participants?"""
    for p in collision.participants:
        if player_id is not None and p.player_id == player_id:
            return True
        same_video = video_id is None or collision.video_id is None or video_id == collision.video_id
        if track_id is not None and p.track_id == track_id and p.player_id is None and same_video:
            return True
    return False


def _find_preceding_collision(db: Session, match_id: Optional[int], player_id: Optional[int],
                              track_id: Optional[int], video_id: Optional[int],
                              video_time_sec: Optional[float]) -> Optional[CollisionEvent]:
    """
    The collision that (most plausibly) came right before a fall of THIS player: same match, the player
    was a participant, and the fall happened no more than COLLISION_TO_INJURY_WINDOW_SEC after the
    collision ended. Only the player who fell is linked - the other participant is never marked injured.
    """
    if match_id is None or video_time_sec is None or (player_id is None and track_id is None):
        return None
    window = settings.COLLISION_TO_INJURY_WINDOW_SEC
    candidates = (
        _collision_query(db)
        .filter(
            CollisionEvent.match_session_id == match_id,
            CollisionEvent.video_time_sec <= video_time_sec + 0.5,
            CollisionEvent.video_time_sec >= video_time_sec - window,
        )
        .order_by(CollisionEvent.video_time_sec.desc())
        .all()
    )
    for collision in candidates:
        if _participant_matches(collision, player_id, track_id, video_id):
            return collision
    return None


def _validated_collision_link(db: Session, collision_id: int, match_id: Optional[int], player_id: Optional[int],
                              track_id: Optional[int], video_id: Optional[int]) -> CollisionEvent:
    collision = _collision_or_404(db, collision_id)
    if match_id is not None and collision.match_session_id != match_id:
        raise HTTPException(status_code=400, detail="Collision belongs to a different match")
    if not _participant_matches(collision, player_id, track_id, video_id):
        raise HTTPException(status_code=400, detail="The injured player was not a participant of that collision")
    return collision


# ===========================================================================
# AUTH ROUTER
# ===========================================================================
auth_router = APIRouter(tags=["auth"])


@auth_router.post("/auth/login", response_model=TokenResponse)
def login(request: LoginRequest, db: Session = Depends(get_db)):
    return login_user(db, request)


@auth_router.get("/auth/me", response_model=UserRead)
def read_current_user(user: User = Depends(get_current_active_user)):
    return UserRead.model_validate(user)


@auth_router.post("/auth/change-password", response_model=MessageResponse)
def update_current_password(
    request: PasswordChangeRequest,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    change_password(db, user, request.current_password, request.new_password)
    return MessageResponse(message="Password changed successfully.")


@auth_router.post("/auth/register", response_model=UserRead, status_code=201)
def register(data: UserCreate, current: Optional[User] = Depends(get_optional_user), db: Session = Depends(get_db)):
    """
    Admin: may create any account.
    Anyone else: only when ALLOW_PUBLIC_REGISTRATION=true, and then only a COACH account that stays
    inactive until an admin activates it (so nobody can grant themselves access to a team).
    """
    if current is not None and current.role == ROLE_ADMIN:
        user_data = data
    elif current is not None:
        raise HTTPException(status_code=403, detail="Only admin can create accounts")
    elif not settings.ALLOW_PUBLIC_REGISTRATION:
        raise HTTPException(status_code=403, detail="Registration is disabled. Ask an admin to create your account")
    elif data.role != ROLE_COACH:
        raise HTTPException(status_code=403, detail="Self-registration is only available for coach accounts")
    else:
        user_data = data.model_copy(update={"active": False})
    user = create_user(db, user_data)
    return UserRead.model_validate(user)


users_router = APIRouter(tags=["users"])


@users_router.get("/users", response_model=List[UserRead])
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [UserRead.model_validate(u) for u in db.query(User).order_by(User.id).all()]


@users_router.patch("/users/{user_id}", response_model=UserRead)
def patch_user(user_id: int, data: UserUpdate, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserRead.model_validate(update_user(db, user, data))


# ===========================================================================
# MATCHES ROUTER
# ===========================================================================
matches_router = APIRouter(tags=["matches"])


@matches_router.post("/matches")
def create_match(
    request: MatchCreateRequest,
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    _validate_teams(db, request.team_a_id, request.team_b_id)
    match = MatchSession(name=request.name, video_source=request.video_source,
                         team_a_id=request.team_a_id, team_b_id=request.team_b_id, date=request.date)
    db.add(match)
    db.commit()
    db.refresh(match)
    logger.info("match created id=%s by=%s teams=%s/%s", match.id, principal.label, match.team_a_id, match.team_b_id)
    return {"message": "Match created", "match_session_id": match.id, "match": MatchRead.model_validate(match)}


@matches_router.get("/matches", response_model=List[MatchRead])
def list_matches(
    status: Optional[str] = Query(None, description="CREATED, VIDEO_UPLOADED, PROCESSING, COMPLETED, or FAILED"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    query = _scope_matches_to_user(db.query(MatchSession), current_user)
    if status:
        query = query.filter(MatchSession.status == status.strip().upper())
    matches = query.order_by(MatchSession.date.desc(), MatchSession.id.desc()).all()
    return [MatchRead.model_validate(m) for m in matches]


@matches_router.get("/matches/{match_id}", response_model=MatchRead)
def get_match(match_id: int, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    match = _match_or_404(db, match_id)
    _ensure_match_access(match, current_user)
    return MatchRead.model_validate(match)


@matches_router.patch("/matches/{match_id}", response_model=MatchRead)
def patch_match(match_id: int, data: MatchUpdate, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    match = _match_or_404(db, match_id)
    changes = data.model_dump(exclude_unset=True)
    team_a = changes.get("team_a_id", match.team_a_id)
    team_b = changes.get("team_b_id", match.team_b_id)
    _validate_teams(db, team_a, team_b)
    for field in ("name", "video_source", "team_a_id", "team_b_id", "date"):
        if field in changes:
            setattr(match, field, changes[field])
    new_status = changes.get("status")
    if new_status and new_status not in _config.MATCH_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {list(_config.MATCH_STATUSES)}")
    if new_status and new_status != match.status:
        match.status = new_status
    db.commit()
    db.refresh(match)
    return MatchRead.model_validate(match)


@matches_router.post("/matches/{match_id}/start", response_model=MatchRead)
def start_match(match_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    match = _match_or_404(db, match_id)
    if match.status == MATCH_FINISHED:
        raise HTTPException(status_code=409, detail="A completed match cannot be processed again")
    video = db.query(Video).filter(
        Video.match_session_id == match.id,
        Video.status.in_(("UPLOADED", "PROCESSING", "FAILED")),
    ).order_by(Video.id.desc()).first()
    if video is None:
        raise HTTPException(status_code=409, detail="Upload a video before starting AI processing")
    from video_processing import enqueue_video, is_processing
    if video.status == "PROCESSING" and is_processing(video.id):
        return MatchRead.model_validate(match)
    video.status = "PROCESSING"
    video.error_message = None
    match.status = MATCH_LIVE
    db.commit()
    try:
        enqueue_video(video.id)
    except Exception as exc:
        db.rollback()
        video = _video_or_404(db, video.id)
        match = _match_or_404(db, match_id)
        video.status = "FAILED"
        video.error_message = f"Processing could not be started: {type(exc).__name__}"
        match.status = "FAILED"
        db.commit()
        raise HTTPException(status_code=503, detail="The AI processing worker could not be started")
    db.refresh(match)
    return MatchRead.model_validate(match)


@matches_router.post("/matches/{match_id}/end", response_model=MatchRead)
def end_match(match_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    match = _match_or_404(db, match_id)
    if match.status != MATCH_FINISHED:
        if match.status not in (MATCH_LIVE, "VIDEO_UPLOADED"):
            raise HTTPException(status_code=409, detail="Only a processing match can be ended")
        match.status = MATCH_FINISHED
        db.commit()
        db.refresh(match)
    return MatchRead.model_validate(match)


@matches_router.get("/matches/{match_id}/summary")
def match_summary(match_id: int, _: User = Depends(require_staff), db: Session = Depends(get_db)):
    """Dashboard counters for one match (staff only: coaches would otherwise see other teams' data)."""
    match = _match_or_404(db, match_id)
    total, falls, high, unresolved = db.query(
        func.count(InjuryEvent.id),
        func.coalesce(func.sum(case((func.upper(InjuryEvent.event_type) == EVENT_TYPE_FALL, 1), else_=0)), 0),
        func.coalesce(func.sum(case((InjuryEvent.risk.in_((RISK_HIGH, RISK_CRITICAL)), 1), else_=0)), 0),
        func.coalesce(func.sum(case((or_(InjuryEvent.resolved.is_(False), InjuryEvent.resolved.is_(None)), 1),
                                    else_=0)), 0),
    ).filter(InjuryEvent.match_session_id == match_id).one()
    collisions = db.query(func.count(CollisionEvent.id)).filter(
        CollisionEvent.match_session_id == match_id).scalar() or 0
    tracks, identified = db.query(
        func.count(PlayerTrack.id),
        func.coalesce(func.sum(case((PlayerTrack.identity_status == IDENTITY_CONFIRMED, 1), else_=0)), 0),
    ).filter(PlayerTrack.match_session_id == match_id).one()
    videos = db.query(func.count(Video.id)).filter(Video.match_session_id == match_id).scalar() or 0
    unread_alerts = db.query(func.count(Alert.id)).filter(
        Alert.is_read.is_(False),
        or_(Alert.injury_event.has(InjuryEvent.match_session_id == match_id),
            Alert.collision_event.has(CollisionEvent.match_session_id == match_id)),
    ).scalar() or 0
    return {"match_session_id": match.id, "status": match.status, "events": int(total), "falls": int(falls),
            "high_risk_events": int(high), "unresolved_events": int(unresolved), "collisions": int(collisions),
            "tracks": int(tracks), "identified_tracks": int(identified), "unidentified_tracks": int(tracks - identified),
            "videos": int(videos), "unread_alerts": int(unread_alerts)}


# ===========================================================================
# EVENTS ROUTER (falls / injuries) - original endpoints, same shapes
# ===========================================================================
events_router = APIRouter(tags=["events"])


def _merge_duplicate_event(event: InjuryEvent, request: EventCreateRequest, player: Optional[Player],
                           status: str) -> None:
    """The same physical fall re-reported: strengthen the stored row instead of adding another one."""
    event.detection_count = (event.detection_count or 1) + 1
    event.confidence = _max(event.confidence, request.confidence)
    event.risk_score = _max(event.risk_score, request.risk_score)
    event.movement = _max(event.movement, request.movement)
    event.risk = _escalate(event.risk, request.risk)
    if request.clip_path and not event.clip_path:
        event.clip_path = request.clip_path
    if event.player_id is None and player is not None:       # identity completed later on the same event
        event.player_id = player.id
        event.identified_at = _now()
        event.identified_by = request.identified_by
        event.id_detail = request.id_detail
        event.id_confidence = request.id_confidence
        logger.info("event %s identity completed -> player %s (%s)", event.id, player.id, request.identified_by)


def _find_duplicate_event(db: Session, request: EventCreateRequest, player: Optional[Player]) -> Optional[InjuryEvent]:
    if request.dedupe_key:
        found = db.query(InjuryEvent).filter(
            InjuryEvent.match_session_id == request.match_session_id,
            InjuryEvent.dedupe_key == request.dedupe_key,
        ).first()
        if found is not None:
            return found
    if (request.event_type != EVENT_TYPE_FALL or request.source != EVENT_SOURCE_AI
            or request.match_session_id is None or request.video_time_sec is None):
        return None
    query = db.query(InjuryEvent).filter(
        InjuryEvent.match_session_id == request.match_session_id,
        InjuryEvent.event_type == EVENT_TYPE_FALL,
        InjuryEvent.source == EVENT_SOURCE_AI,
        InjuryEvent.video_time_sec.isnot(None),
        func.abs(InjuryEvent.video_time_sec - request.video_time_sec) <= settings.FALL_DEDUP_WINDOW_SEC,
    )
    if player is not None:
        same_track_unknown = (
            and_(InjuryEvent.player_id.is_(None), InjuryEvent.track_id == request.track_id)
            if request.track_id is not None else None
        )
        conditions = [InjuryEvent.player_id == player.id]
        if same_track_unknown is not None:
            conditions.append(same_track_unknown)
        query = query.filter(or_(*conditions))
    elif request.track_id is not None:
        query = query.filter(InjuryEvent.player_id.is_(None), InjuryEvent.track_id == request.track_id)
    else:
        return None
    if request.video_id is not None:
        query = query.filter(or_(InjuryEvent.video_id.is_(None), InjuryEvent.video_id == request.video_id))
    return query.order_by(InjuryEvent.video_time_sec.desc()).first()


@events_router.post("/events", response_model=EventCreateResponse)
def create_event(
    request: EventCreateRequest,
    response: Response,
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    match = _match_or_404(db, request.match_session_id) if request.match_session_id is not None else None
    player = _player_or_404(db, request.player_id) if request.player_id is not None else None

    if player is not None:
        _ensure_player_usable(player, request.source)
        _ensure_player_in_match(match, player)
    elif request.source == EVENT_SOURCE_AI and not settings.ALLOW_UNIDENTIFIED_EVENTS:
        raise HTTPException(status_code=422, detail="Events for unidentified players are disabled")
    _validate_video(db, request.video_id, request.match_session_id)
    _validate_player_track(db, request.player_track_id, player, request.match_session_id)

    status = _effective_status(player, None, request.id_confidence, request.identified_by)
    if player is not None and request.source == EVENT_SOURCE_AI and status != IDENTITY_CONFIRMED:
        raise HTTPException(status_code=422, detail="AI player_id requires confirmed identity confidence")

    duplicate = _find_duplicate_event(db, request, player)
    if duplicate is not None:
        _merge_duplicate_event(duplicate, request, player, status)
        db.commit()
        logger.info("duplicate event merged into %s (detection_count=%s)", duplicate.id, duplicate.detection_count)
        return EventCreateResponse(message="Duplicate event merged", event_id=duplicate.id, duplicate=True)

    collision_id = None
    if request.collision_event_id is not None:
        collision_id = _validated_collision_link(
            db, request.collision_event_id, request.match_session_id,
            request.player_id, request.track_id, request.video_id).id
    elif request.event_type == EVENT_TYPE_FALL and request.source == EVENT_SOURCE_AI:
        linked = _find_preceding_collision(db, request.match_session_id, request.player_id, request.track_id,
                                           request.video_id, request.video_time_sec)
        collision_id = linked.id if linked is not None else None

    dedupe_key = request.dedupe_key
    if dedupe_key is None and request.match_session_id is not None and request.source == EVENT_SOURCE_AI:
        moment = request.frame_number if request.frame_number is not None else (
            int(request.video_time_sec * 100) if request.video_time_sec is not None else None)
        who = f"p{player.id}" if player is not None else (f"t{request.track_id}" if request.track_id is not None else None)
        if moment is not None and who is not None:
            dedupe_key = f"{request.event_type.lower()}-m{request.match_session_id}-v{request.video_id or 0}-{who}-f{moment}"

    event = InjuryEvent(
        player_id=request.player_id,
        match_session_id=request.match_session_id,
        event_type=request.event_type,
        region=request.region,
        risk=request.risk,
        movement=request.movement,
        note=request.injury_note,
        injury_note=request.injury_note,
        clip_path=request.clip_path,
        source=request.source,
        track_id=request.track_id,
        video_time_sec=request.video_time_sec,
        video_id=request.video_id,
        player_track_id=request.player_track_id,
        collision_event_id=collision_id,
        frame_number=request.frame_number,
        location_x=request.location_x,
        location_y=request.location_y,
        confidence=request.confidence,
        risk_score=request.risk_score,
        side=request.side,
        detection_count=1,
        dedupe_key=dedupe_key,
    )
    if player is not None:
        event.identified_at = _now()
        event.identified_by = request.identified_by
        event.id_detail = request.id_detail
        event.id_confidence = request.id_confidence

    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(InjuryEvent).filter(InjuryEvent.dedupe_key == dedupe_key).first() if dedupe_key else None
        if existing is None:
            raise
        return EventCreateResponse(message="Duplicate event merged", event_id=existing.id, duplicate=True)
    db.refresh(event)
    logger.info("event created id=%s type=%s player=%s track=%s match=%s collision=%s by=%s",
                event.id, event.event_type, event.player_id, event.track_id, event.match_session_id,
                event.collision_event_id, principal.label)
    return EventCreateResponse(message="Event recorded", event_id=event.id, duplicate=False)


@events_router.patch("/events/{event_id}/ai-update")
def ai_update_event(
    event_id: int,
    request: EventAIUpdateRequest,
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    event = _event_or_404(db, event_id)
    player: Optional[Player] = None

    if request.player_id is not None:
        player = _player_or_404(db, request.player_id)
        if (request.identified_by != "manual" and request.id_confidence is not None
                and request.id_confidence < settings.IDENTITY_CONFIRM_MIN_CONFIDENCE):
            raise HTTPException(status_code=422, detail="AI player_id requires confirmed identity confidence")
        if event.player_id is not None and event.player_id != player.id:
            # Refuse the flip-flop (Arun -> Karthik). A deliberate correction goes through /assign.
            raise HTTPException(status_code=409,
                                detail="Event is already attributed to another player; use /assign to correct it")
        _ensure_player_usable(player, EVENT_SOURCE_AI)
        _ensure_player_in_match(event.match, player)
        _validate_player_track(db, request.player_track_id, player, event.match_session_id)
        event.player_id = player.id
        event.identified_at = _now()
        event.identified_by = request.identified_by
        event.id_detail = request.id_detail
        event.id_confidence = request.id_confidence

    if request.player_track_id is not None and player is None:
        _validate_player_track(db, request.player_track_id, event.player, event.match_session_id)
    if request.player_track_id is not None:
        event.player_track_id = request.player_track_id
    if request.clip_path is not None:
        event.clip_path = request.clip_path
    if request.injury_note is not None:
        event.injury_note = request.injury_note

    db.commit()
    db.refresh(event)
    logger.info("event updated id=%s by=%s player=%s", event.id, principal.label, event.player_id)
    return {"message": "Event updated", "event": _event_read(event)}


@events_router.patch("/events/{event_id}/assign")
def assign_player(
    event_id: int,
    request: AssignPlayerRequest,
    current_user: User = Depends(require_roles(ROLE_MEDICAL, ROLE_ADMIN,
                                               detail="Only medical staff or admin can assign players")),
    db: Session = Depends(get_db),
):
    event = _event_or_404(db, event_id)
    player = _player_or_404(db, request.player_id)
    _ensure_player_in_match(event.match, player)

    previous = event.player.name if event.player is not None else "Unidentified"
    event.player_id = player.id
    event.identified_at = _now()
    event.identified_by = "manual"
    event.id_detail = "assigned by " + current_user.username + " (was: " + previous + ")"
    event.id_confidence = None
    event.player_track_id = None   # the old track link described the previous attribution

    if event.collision_event_id is not None:
        collision = _collision_or_404(db, event.collision_event_id)
        if not _participant_matches(collision, player.id, event.track_id, event.video_id):
            logger.info("event %s re-assigned to player %s who was not in collision %s - link removed",
                        event.id, player.id, collision.id)
            event.collision_event_id = None

    db.commit()
    db.refresh(event)
    logger.info("event %s assigned to player %s by %s (was: %s)", event.id, player.id, current_user.username, previous)
    return {"message": "Player assigned", "event": _event_read(event)}


@events_router.post("/events/manual")
def create_manual_event(
    request: ManualEventRequest,
    current_user: User = Depends(require_roles(ROLE_MEDICAL, detail="Only medical staff can log manual events")),
    db: Session = Depends(get_db),
):
    player = _player_or_404(db, request.player_id)
    match = _match_or_404(db, request.match_session_id) if request.match_session_id is not None else None
    _ensure_player_in_match(match, player)

    event = InjuryEvent(
        player_id=player.id,
        match_session_id=request.match_session_id,
        event_type=request.event_type,
        note=request.note,
        risk=request.risk,
        source=EVENT_SOURCE_MANUAL,
        identified_by="manual",
        id_detail="logged by " + current_user.username,
        identified_at=_now(),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    logger.info("manual event %s logged by %s for player %s", event.id, current_user.username, player.id)
    return {"message": "Manual event recorded", "event_id": event.id}


@events_router.get("/events", response_model=List[InjuryEventRead])
def list_events(
    match_id: Optional[int] = Query(None),
    player_id: Optional[int] = Query(None),
    event_type: Optional[str] = Query(None),
    risk: Optional[str] = Query(None),
    resolved: Optional[bool] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Unassigned events are staff-only; coaches and players see only associated records."""
    query = _event_query(db).outerjoin(Player, InjuryEvent.player_id == Player.id)

    if current_user.role in STAFF_ROLES:
        pass
    elif current_user.role == ROLE_COACH:
        query = query.filter(Player.team_id == current_user.team_id)
    elif current_user.role == ROLE_PLAYER:
        _player_profile_for_user(db, current_user)
        query = query.filter(InjuryEvent.player_id == current_user.player_id)
    else:
        raise HTTPException(status_code=403, detail="Unknown role")

    if match_id is not None:
        query = query.filter(InjuryEvent.match_session_id == match_id)
    if player_id is not None:
        query = query.filter(InjuryEvent.player_id == player_id)
    if event_type:
        query = query.filter(func.upper(InjuryEvent.event_type) == event_type.strip().upper())
    if risk:
        query = query.filter(InjuryEvent.risk == risk.strip().upper())
    if resolved is not None:
        query = query.filter(InjuryEvent.resolved.is_(resolved) if resolved else
                             or_(InjuryEvent.resolved.is_(False), InjuryEvent.resolved.is_(None)))

    query = query.order_by(InjuryEvent.timestamp.desc(), InjuryEvent.id.desc()).offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return [_event_read(e) for e in query.all()]


@events_router.get("/events/{event_id}", response_model=InjuryEventRead)
def get_event(event_id: int, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    event = _event_or_404(db, event_id)
    if current_user.role == ROLE_COACH:
        if event.player is None:
            raise HTTPException(status_code=403, detail="Unassigned incidents are restricted to staff")
        ensure_team_access(current_user, event.player.team_id)
    elif current_user.role == ROLE_PLAYER:
        _player_profile_for_user(db, current_user)
        if event.player_id != current_user.player_id:
            raise HTTPException(status_code=403, detail="You can only access your own incidents")
    elif current_user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Unknown role")
    return _event_read(event)


@events_router.get("/events/{event_id}/medical-assessment", response_model=MedicalAssessmentRead)
def get_event_medical_assessment(
    event_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    event = _event_or_404(db, event_id)
    if current_user.role == ROLE_COACH:
        if event.player is None:
            raise HTTPException(status_code=403, detail="Unassigned incidents are restricted to staff")
        ensure_team_access(current_user, event.player.team_id)
    elif current_user.role == ROLE_PLAYER:
        _player_profile_for_user(db, current_user)
        if event.player_id != current_user.player_id:
            raise HTTPException(status_code=403, detail="You can only access your own assessments")
    elif current_user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Unknown role")

    collision = event.collision_event
    return build_medical_assessment(
        event=event,
        collision=collision,
        event_clip_url=build_clip_url(event.clip_path) if event.clip_path else None,
        collision_clip_url=build_clip_url(collision.clip_path)
        if collision is not None and collision.clip_path else None,
    )


@events_router.patch("/events/{event_id}/resolve")
def resolve_event(event_id: int, current_user: User = Depends(get_current_active_user),
                  db: Session = Depends(get_db)):
    """admin/medical: any event. coach: only events of their own team's players."""
    event = _event_or_404(db, event_id)
    if current_user.role not in STAFF_ROLES:
        if current_user.role != ROLE_COACH or event.player is None:
            raise HTTPException(status_code=403, detail="You cannot resolve this event")
        ensure_team_access(current_user, event.player.team_id, "You can only resolve events of your own team")
    if not event.resolved:
        event.resolved = True
        event.resolved_by_id = current_user.id
        event.resolved_at = _now()
        db.commit()
        logger.info("event %s resolved by %s", event.id, current_user.username)
    return {"message": "Event marked as resolved"}


@events_router.delete("/admin/players/clear")
def clear_all_injury_events(current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Original admin action: delete every injury event (and their alerts). Players, matches and collisions stay."""
    deleted = db.query(InjuryEvent).delete(synchronize_session=False)
    db.commit()
    logger.warning("ALL injury events deleted (%s rows) by admin %s", deleted, current_user.username)
    return {"message": "All injury events deleted. Players were kept.", "deleted_events": deleted}


# ===========================================================================
# COLLISIONS ROUTER
# ===========================================================================
collisions_router = APIRouter(tags=["collisions"])


def _same_participant(new, old, same_video: bool) -> bool:
    """Same person in both reports: same confirmed player, or - when ids are not both known - same track."""
    if new.player_id is not None and old.player_id is not None:
        return new.player_id == old.player_id
    return same_video and new.track_id == old.track_id


def _same_pair(new_parts, old_parts, same_video: bool) -> bool:
    a, b = new_parts
    c, d = old_parts
    return ((_same_participant(a, c, same_video) and _same_participant(b, d, same_video))
            or (_same_participant(a, d, same_video) and _same_participant(b, c, same_video)))


def _find_same_collision(db: Session, data: CollisionEventCreate) -> Optional[CollisionEvent]:
    """Find a likely duplicate using only collision fields stored by this schema."""
    window = settings.COLLISION_DEDUP_WINDOW_SEC
    if data.match_session_id is None or data.video_time_sec is None:
        return None
    candidates = (
        _collision_query(db)
        .filter(
            CollisionEvent.match_session_id == data.match_session_id,
            CollisionEvent.video_time_sec.isnot(None),
            func.abs(CollisionEvent.video_time_sec - data.video_time_sec) <= window,
        )
        .order_by(CollisionEvent.video_time_sec.desc())
        .all()
    )
    for candidate in candidates:
        same_video = data.video_id is None or candidate.video_id is None or data.video_id == candidate.video_id
        if len(candidate.participants) == 2 and _same_pair(
                [part for _, part in data.slotted_participants()], candidate.participants, same_video):
            return candidate
    return None


def _merge_collision(db: Session, existing: CollisionEvent, data: CollisionEventCreate) -> None:
    """Merge data into fields that the existing collision table actually stores."""
    existing.detection_count = (existing.detection_count or 1) + 1
    existing.confidence = _max(existing.confidence, data.confidence)
    existing.risk = _escalate(existing.risk, data.risk)
    if data.clip_path and not existing.clip_path:
        existing.clip_path = data.clip_path
    same_video = data.video_id is None or existing.video_id is None or data.video_id == existing.video_id
    for _, new in data.slotted_participants():
        for old in existing.participants:
            if not _same_participant(new, old, same_video):
                continue
            other = next(p for p in existing.participants if p is not old)
            if old.player_id is None and new.player_id is not None and new.player_id != other.player_id:
                old.player_id = new.player_id                      # unknown -> confidently identified
                old.player_track_id = new.player_track_id or old.player_track_id
                old.id_confidence = new.id_confidence
                old.identified_by = new.identified_by
                old.id_detail = new.id_detail
                logger.info("collision %s slot %s identity completed -> player %s", existing.id, old.slot, new.player_id)
            break


@collisions_router.post("/collisions", response_model=CollisionCreateResponse)
def create_collision(
    data: CollisionEventCreate,
    response: Response,
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Record one physical collision between exactly two participants. Re-reports of the same collision
    (same dedupe_key, or same two people within COLLISION_DEDUP_WINDOW_SEC) are merged, not duplicated.
    """
    if data.match_session_id is None:
        raise HTTPException(status_code=422, detail="match_session_id is required for a collision")
    match = _match_or_404(db, data.match_session_id)
    _validate_video(db, data.video_id, data.match_session_id)

    players: Dict[int, Player] = {}
    for slot_no, part in data.slotted_participants():
        if part.player_id is not None:
            if (part.identified_by != "manual" and part.id_confidence is not None
                    and part.id_confidence < settings.IDENTITY_CONFIRM_MIN_CONFIDENCE):
                raise HTTPException(status_code=422, detail="collision player_id requires confirmed identity confidence")
            player = _player_or_404(db, part.player_id)
            _ensure_player_usable(player, EVENT_SOURCE_AI)
            _ensure_player_in_match(match, player)
            players[slot_no] = player
        _validate_player_track(db, part.player_track_id, players.get(slot_no), data.match_session_id)

    existing = None
    if data.dedupe_key:
        existing = _collision_query(db).filter(
            CollisionEvent.match_session_id == data.match_session_id,
            CollisionEvent.dedupe_key == data.dedupe_key,
        ).first()
    if existing is None:
        existing = _find_same_collision(db, data)
    if existing is not None:
        _merge_collision(db, existing, data)
        db.commit()
        logger.info("collision report merged into %s (detection_count=%s)", existing.id, existing.detection_count)
        return CollisionCreateResponse(message="Duplicate collision merged", collision_event_id=existing.id,
                                       duplicate=True)

    first, second = data.participants
    if first.player_id is not None and second.player_id is not None:
        pair = f"p{min(first.player_id, second.player_id)}-p{max(first.player_id, second.player_id)}"
    else:
        pair = f"t{min(first.track_id, second.track_id)}-t{max(first.track_id, second.track_id)}"
    dedupe_key = data.dedupe_key or f"collision-m{data.match_session_id}-v{data.video_id or 0}-{pair}-f{data.frame_number}"

    collision = CollisionEvent(
        match_session_id=data.match_session_id,
        video_id=data.video_id,
        frame_number=data.frame_number,
        video_time_sec=data.video_time_sec,
        location_x=data.location_x,
        location_y=data.location_y,
        location_bbox=data.location_bbox,
        iou=data.iou,
        contact_frames=data.contact_frames,
        event_type=_config.EVENT_TYPE_COLLISION,
        region=data.region,
        source=data.source,
        confidence=data.confidence,
        risk=data.risk,
        dedupe_key=dedupe_key,
        detection_count=1,
        clip_path=data.clip_path,
        risk_score=data.risk_score,
        note=data.note,
    )
    for slot_no, part in data.slotted_participants():
        collision.participants.append(CollisionParticipant(
            slot=slot_no,
            track_id=part.track_id,
            player_id=part.player_id,
            player_track_id=part.player_track_id,
            id_confidence=part.id_confidence,
            identified_by=part.identified_by,
            id_detail=part.id_detail,
            region=part.region,
            movement=part.movement,
            identified_at=_now() if part.player_id is not None else None,
        ))
    db.add(collision)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        found = db.query(CollisionEvent).filter(
            CollisionEvent.match_session_id == data.match_session_id,
            CollisionEvent.dedupe_key == dedupe_key,
        ).first()
        if found is None:
            raise
        return CollisionCreateResponse(message="Duplicate collision merged", collision_event_id=found.id,
                                       duplicate=True)
    db.refresh(collision)
    response.status_code = 201
    logger.info("collision created id=%s match=%s tracks=%s players=%s at=(%.3f,%.3f) t=%.2fs by=%s",
                collision.id, collision.match_session_id,
                [p.track_id for p in collision.participants], [p.player_id for p in collision.participants],
                collision.location_x, collision.location_y, collision.video_time_sec, principal.label)
    return CollisionCreateResponse(message="Collision recorded", collision_event_id=collision.id, duplicate=False)


@collisions_router.get("/collisions", response_model=List[CollisionEventRead])
def list_collisions(
    match_id: Optional[int] = Query(None),
    player_id: Optional[int] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Staff see all collisions; coaches see their team, players see collisions they took part in."""
    query = _collision_query(db)
    if current_user.role in STAFF_ROLES:
        pass
    elif current_user.role == ROLE_COACH:
        team_ids = accessible_team_ids(current_user)
        query = query.filter(exists().where(and_(
            CollisionParticipant.collision_event_id == CollisionEvent.id,
            CollisionParticipant.player_id == Player.id,
            Player.team_id.in_(list(team_ids) or [-1]),
        )))
    elif current_user.role == ROLE_PLAYER:
        _player_profile_for_user(db, current_user)
        query = query.filter(exists().where(and_(
            CollisionParticipant.collision_event_id == CollisionEvent.id,
            CollisionParticipant.player_id == current_user.player_id,
        )))
    else:
        raise HTTPException(status_code=403, detail="Unknown role")
    if match_id is not None:
        query = query.filter(CollisionEvent.match_session_id == match_id)
    if player_id is not None:
        query = query.filter(exists().where(and_(
            CollisionParticipant.collision_event_id == CollisionEvent.id,
            CollisionParticipant.player_id == player_id,
        )))
    rows = (query.order_by(CollisionEvent.video_time_sec.desc(), CollisionEvent.id.desc())
            .offset(offset).limit(limit).all())
    return [_collision_read(c, current_user) for c in rows]


@collisions_router.get("/collisions/{collision_id}", response_model=CollisionEventRead)
def get_collision(collision_id: int, current_user: User = Depends(get_current_active_user),
                  db: Session = Depends(get_db)):
    collision = _collision_or_404(db, collision_id)
    if current_user.role in STAFF_ROLES:
        pass
    elif current_user.role == ROLE_COACH:
        team_ids = accessible_team_ids(current_user)
        involved = any(p.player is not None and p.player.team_id in team_ids for p in collision.participants)
        if not involved:
            raise HTTPException(status_code=403, detail="You can only view collisions involving your own team")
    elif current_user.role == ROLE_PLAYER:
        _player_profile_for_user(db, current_user)
        if not any(p.player_id == current_user.player_id for p in collision.participants):
            raise HTTPException(status_code=403, detail="You can only access collisions you took part in")
    else:
        raise HTTPException(status_code=403, detail="Unknown role")
    return _collision_read(collision, current_user)


@collisions_router.get("/collisions/{collision_id}/medical-assessment", response_model=MedicalAssessmentRead)
def get_collision_medical_assessment(
    collision_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    collision = _collision_or_404(db, collision_id)
    if current_user.role == ROLE_COACH:
        team_ids = accessible_team_ids(current_user) or []
        involved = any(part.player is not None and part.player.team_id in team_ids
                       for part in collision.participants)
        if not involved:
            raise HTTPException(status_code=403, detail="You can only view collisions involving your own team")
    elif current_user.role == ROLE_PLAYER:
        _player_profile_for_user(db, current_user)
        if not any(part.player_id == current_user.player_id for part in collision.participants):
            raise HTTPException(status_code=403, detail="You can only access your own assessments")
    elif current_user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Unknown role")
    return build_medical_assessment(
            collision=collision,
            collision_clip_url=build_clip_url(collision.clip_path) if collision.clip_path else None,
    )


@collisions_router.patch("/collisions/{collision_id}", response_model=CollisionEventRead)
def update_collision(
    collision_id: int,
    data: CollisionEventUpdate,
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """The AI extends / closes a collision it already reported. Evidence only ever strengthens."""
    collision = _collision_or_404(db, collision_id)
    collision.confidence = _max(collision.confidence, data.confidence)
    collision.risk = _escalate(collision.risk, data.risk)
    if data.risk_score is not None:
        collision.risk_score = _max(collision.risk_score, data.risk_score)
    if data.resolved is not None:
        collision.resolved = data.resolved
        collision.resolved_at = _now() if data.resolved else None
    if data.clip_path is not None:
        collision.clip_path = data.clip_path
    if data.note is not None:
        collision.note = data.note
    db.commit()
    db.refresh(collision)
    logger.info("collision %s updated by %s (resolved=%s)", collision.id, principal.label, collision.resolved)
    return _collision_read(_collision_or_404(db, collision_id))


@collisions_router.patch("/collisions/{collision_id}/assign", response_model=CollisionEventRead)
def assign_collision_participant(
    collision_id: int,
    data: CollisionParticipantAssign,
    slot_number: int = Query(..., alias="slot", ge=1, le=2),
    current_user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Staff attach a CONFIRMED player to a participant that the AI left Unknown (or correct one)."""
    collision = _collision_or_404(db, collision_id)
    slot = next((p for p in collision.participants if p.slot == slot_number), None)
    other = next((p for p in collision.participants if p.slot != slot_number), None)
    if slot is None or other is None:
        raise HTTPException(status_code=404, detail="Collision participant not found")
    player = _player_or_404(db, data.player_id)
    _ensure_player_in_match(collision.match, player)
    if other.player_id == player.id:
        raise HTTPException(status_code=409, detail="That player is already the other participant of this collision")
    _validate_player_track(db, data.player_track_id, player, collision.match_session_id)

    slot.player_id = player.id
    slot.player_track_id = data.player_track_id
    slot.id_confidence = data.id_confidence
    slot.identified_by = data.identified_by or "manual"
    slot.id_detail = data.id_detail
    slot.identified_at = _now()
    db.commit()
    logger.info("collision %s slot %s assigned to player %s by %s", collision.id, slot_number, player.id,
                current_user.username)
    return _collision_read(_collision_or_404(db, collision_id))


# ===========================================================================
# TRACKS ROUTER  (track_id <-> player_id link, one row per track per video)
# ===========================================================================
tracks_router = APIRouter(tags=["tracks"])


def _new_track_cache() -> Dict[str, Any]:
    return {"matches": {}, "players": {}, "videos": set()}


def _upsert_track_row(db: Session, data: PlayerTrackUpsert, cache: Dict[str, Any]):
    """Create or update the single row for (match, video, tracker id). Returns (track, created)."""
    match = cache["matches"].get(data.match_session_id)
    if match is None:
        match = _match_or_404(db, data.match_session_id)
        cache["matches"][data.match_session_id] = match
    video_key = (data.video_id, data.match_session_id)
    if video_key not in cache["videos"]:
        _validate_video(db, data.video_id, data.match_session_id)
        cache["videos"].add(video_key)
    player = None
    if data.player_id is not None:
        player = cache["players"].get(data.player_id)
        if player is None:
            player = _player_or_404(db, data.player_id)
            cache["players"][data.player_id] = player
        _ensure_player_usable(player, EVENT_SOURCE_AI)
        _ensure_player_in_match(match, player)
        if (data.identity_status not in (None, IDENTITY_CONFIRMED)
                or (data.id_confidence is not None
                    and data.id_confidence < settings.IDENTITY_CONFIRM_MIN_CONFIDENCE)):
            raise HTTPException(status_code=422, detail="player_id requires a confirmed track identity")
    if data.team_id is not None and db.query(Team.id).filter(Team.id == data.team_id).first() is None:
        raise HTTPException(status_code=404, detail=f"Team {data.team_id} not found")
    if player is not None and data.team_id is not None and data.team_id != player.team_id:
        raise HTTPException(status_code=400, detail="team_id must match the identified player's team")

    query = db.query(PlayerTrack).filter(PlayerTrack.match_session_id == data.match_session_id,
                                         PlayerTrack.track_id == data.track_id)
    track = query.first()
    created = track is None

    if created:
        track = PlayerTrack(match_session_id=data.match_session_id, video_id=data.video_id, track_id=data.track_id)
        db.add(track)
    elif track.player_id is not None and data.player_id is not None and track.player_id != data.player_id:
        # CONFIRMED -> a different CONFIRMED player is an identity flip: never swap names silently.
        raise HTTPException(status_code=409, detail="Track is already confirmed as another player")
    if not created and track.video_id not in (None, data.video_id) and data.video_id is not None:
        raise HTTPException(status_code=409, detail="Tracker id is already linked to another video in this match")
    if track.video_id is None:
        track.video_id = data.video_id

    if data.player_id is not None:
        track.player_id = data.player_id
        track.team_id = data.team_id if data.team_id is not None else player.team_id
        track.identity_status = data.identity_status or IDENTITY_CONFIRMED
        track.id_confidence = data.id_confidence
        track.identified_by = data.identified_by
        track.id_detail = data.id_detail
        track.identified_at = _now()
    elif data.identity_status == IDENTITY_UNKNOWN:
        track.player_id = None
        track.team_id = data.team_id
        track.identity_status = IDENTITY_UNKNOWN
        track.id_confidence = data.id_confidence
        track.identified_by = data.identified_by
        track.id_detail = data.id_detail
        track.identified_at = None
    elif track.player_id is None:
        track.team_id = data.team_id
        track.identity_status = data.identity_status or IDENTITY_UNKNOWN
        track.id_confidence = data.id_confidence
        track.identified_by = data.identified_by
        track.id_detail = data.id_detail
    if data.predecessor_track_id is not None:
        predecessor = db.query(PlayerTrack).filter(
            PlayerTrack.match_session_id == data.match_session_id,
            PlayerTrack.track_id == data.predecessor_track_id,
        ).first()
        if predecessor is None:
            raise HTTPException(status_code=404, detail="Predecessor track not found in this match")
        if player is not None and predecessor.player_id not in (None, player.id):
            raise HTTPException(status_code=409, detail="Predecessor track belongs to a different player")
        track.predecessor_id = predecessor.id
    if data.first_frame is not None:
        track.first_frame = data.first_frame if track.first_frame is None else min(track.first_frame, data.first_frame)
    if data.first_seen_sec is not None:
        track.first_seen_sec = data.first_seen_sec if track.first_seen_sec is None else min(track.first_seen_sec, data.first_seen_sec)
    if data.last_frame is not None:
        track.last_frame = data.last_frame if track.last_frame is None else max(track.last_frame, data.last_frame)
    if data.last_seen_sec is not None:
        track.last_seen_sec = data.last_seen_sec if track.last_seen_sec is None else max(track.last_seen_sec, data.last_seen_sec)
    track.tracking_confidence = _max(track.tracking_confidence, data.tracking_confidence)
    return track, created


@tracks_router.put("/tracks", response_model=PlayerTrackRead)
def upsert_track(
    data: PlayerTrackUpsert,
    response: Response,
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    track, created = _upsert_track_row(db, data, _new_track_cache())
    if created:
        response.status_code = 201
    db.commit()
    db.refresh(track)
    logger.info("track upsert match=%s track_id=%s -> player_id=%s status=%s by=%s", track.match_session_id,
                track.track_id, track.player_id, track.identity_status, principal.label)
    return PlayerTrackRead.model_validate(track)


@tracks_router.put("/tracks/bulk")
def upsert_tracks_bulk(
    data: List[PlayerTrackUpsert],
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """Up to 500 track upserts in ONE transaction. Any invalid item rejects the whole batch (nothing saved)."""
    if not data:
        raise HTTPException(status_code=400, detail="Empty batch")
    if len(data) > 500:
        raise HTTPException(status_code=400, detail="At most 500 tracks per batch")
    cache = _new_track_cache()
    created = updated = 0
    for index, item in enumerate(data):
        try:
            _, was_created = _upsert_track_row(db, item, cache)
        except HTTPException as exc:
            db.rollback()
            raise HTTPException(status_code=exc.status_code, detail=f"item {index}: {exc.detail}") from exc
        created += 1 if was_created else 0
        updated += 0 if was_created else 1
    db.commit()
    logger.info("track bulk upsert created=%s updated=%s by=%s", created, updated, principal.label)
    return {"message": "Tracks upserted", "created": created, "updated": updated}


@tracks_router.get("/matches/{match_id}/tracks", response_model=List[PlayerTrackRead])
def list_match_tracks(
    match_id: int,
    identity_status: Optional[str] = Query(None),
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    _match_or_404(db, match_id)
    query = db.query(PlayerTrack).filter(PlayerTrack.match_session_id == match_id)
    if identity_status:
        query = query.filter(PlayerTrack.identity_status == identity_status.strip().upper())
    return [PlayerTrackRead.model_validate(t) for t in query.order_by(PlayerTrack.track_id).all()]


# ===========================================================================
# PLAYERS / TEAMS ROUTER
# ===========================================================================
roster_router = APIRouter(tags=["players", "teams"])


@roster_router.get("/players", response_model=List[PlayerBrief])
def list_players(
    include_inactive: bool = Query(False),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    query = db.query(Player)
    if current_user.role in STAFF_ROLES:
        pass
    elif current_user.role == ROLE_COACH:
        query = query.filter(Player.team_id == current_user.team_id)
    elif current_user.role == ROLE_PLAYER:
        profile = _player_profile_for_user(db, current_user)
        query = query.filter(Player.id == profile.id)
    else:
        raise HTTPException(status_code=403, detail="Unknown role")
    if not include_inactive:
        query = query.filter(Player.active.is_(True))
    return [PlayerBrief.model_validate(p) for p in query.order_by(Player.team_id, Player.jersey_number).all()]


@roster_router.get("/players/{player_id}", response_model=PlayerRead)
def get_player(player_id: int, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    player = _player_or_404(db, player_id)
    _ensure_player_record_access(current_user, player)
    return PlayerRead.model_validate(player)


@roster_router.get("/players/{player_id}/stats", response_model=PlayerStatsRead)
def get_player_stats(player_id: int, current_user: User = Depends(get_current_active_user),
                     db: Session = Depends(get_db)):
    player = _player_or_404(db, player_id)
    _ensure_player_record_access(current_user, player)
    total, falls, high, resolved, last = (
        db.query(
            func.count(InjuryEvent.id),
            func.coalesce(func.sum(case((func.upper(InjuryEvent.event_type) == EVENT_TYPE_FALL, 1), else_=0)), 0),
            func.coalesce(func.sum(case((InjuryEvent.risk.in_((RISK_HIGH, RISK_CRITICAL)), 1), else_=0)), 0),
            func.coalesce(func.sum(case((InjuryEvent.resolved.is_(True), 1), else_=0)), 0),
            func.max(InjuryEvent.timestamp),
        ).filter(InjuryEvent.player_id == player_id).one()
    )
    collisions = (db.query(func.count(func.distinct(CollisionParticipant.collision_event_id)))
                  .filter(CollisionParticipant.player_id == player_id).scalar()) or 0
    return PlayerStatsRead(id=player.id, name=player.name, jersey_number=player.jersey_number,
                           injury_events=int(total or 0), high_risk_events=int(high))


@roster_router.get("/teams", response_model=List[TeamSummaryRead])
def list_teams(
    include_inactive: bool = Query(False),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """admin/medical: every team. coach: own team only (original rule)."""
    if current_user.role in STAFF_ROLES:
        team_query = db.query(Team)
    elif current_user.role == ROLE_COACH:
        team_query = db.query(Team).filter(Team.id == current_user.team_id)
    else:
        raise HTTPException(status_code=403, detail="Unknown role")

    if not include_inactive:
        team_query = team_query.filter(Team.active.is_(True))
    teams = team_query.order_by(Team.id).all()
    team_ids = [t.id for t in teams]
    player_query = db.query(Player).filter(Player.team_id.in_(team_ids or [-1]))
    if not include_inactive:
        player_query = player_query.filter(Player.active.is_(True))
    players = player_query.order_by(Player.jersey_number).all()

    counts = {
        pid: (total, high)
        for pid, total, high in db.query(
            InjuryEvent.player_id, func.count(InjuryEvent.id),
            func.coalesce(func.sum(case((InjuryEvent.risk.in_((RISK_HIGH, RISK_CRITICAL)), 1), else_=0)), 0),
        ).filter(InjuryEvent.player_id.isnot(None)).group_by(InjuryEvent.player_id).all()
    }
    coaches: Dict[int, List[str]] = {}
    for username, team_id in db.query(User.username, User.team_id).filter(
            User.role == ROLE_COACH, User.team_id.in_(team_ids or [-1])).order_by(User.username).all():
        coaches.setdefault(team_id, []).append(username)

    result: List[TeamSummaryRead] = []
    for team in teams:
        rows: List[PlayerStatsRead] = []
        team_total = team_high = 0
        for p in (x for x in players if x.team_id == team.id):
            total, high = counts.get(p.id, (0, 0))
            team_total += total
            team_high += int(high)
            rows.append(PlayerStatsRead(id=p.id, name=p.name, jersey_number=p.jersey_number,
                                        injury_events=total, high_risk_events=int(high)))
        result.append(TeamSummaryRead(id=team.id, name=team.name, coaches=coaches.get(team.id, []),
                                      player_count=len(rows), injury_events=team_total,
                                      high_risk_events=team_high, players=rows))
    return result


# ---- teams (write side) ------------------------------------------------------------------------
@roster_router.post("/teams", response_model=TeamRead, status_code=201)
def create_team(data: TeamCreate, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    name = data.name.strip()
    if db.query(Team.id).filter(func.lower(Team.name) == name.lower()).first() is not None:
        raise HTTPException(status_code=409, detail="A team with this name already exists")
    team = Team(name=name, short_name=data.short_name, active=data.active)
    db.add(team)
    db.commit()
    db.refresh(team)
    return TeamRead.model_validate(team)


@roster_router.get("/teams/{team_id}", response_model=TeamRead)
def get_team(team_id: int, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    if current_user.role == ROLE_PLAYER:
        raise HTTPException(status_code=403, detail="Player accounts cannot access team details")
    ensure_team_access(current_user, team_id)
    team = db.query(Team).filter(Team.id == team_id).first()
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    return TeamRead.model_validate(team)


@roster_router.patch("/teams/{team_id}", response_model=TeamRead)
def update_team(team_id: int, data: TeamUpdate, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    team = db.query(Team).filter(Team.id == team_id).first()
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    if data.name is not None:
        name = data.name.strip()
        clash = db.query(Team.id).filter(func.lower(Team.name) == name.lower(), Team.id != team_id).first()
        if clash is not None:
            raise HTTPException(status_code=409, detail="A team with this name already exists")
        team.name = name
    changes = data.model_dump(exclude_unset=True)
    for field in ("short_name", "active"):
        if field in changes:
            setattr(team, field, changes[field])
    db.commit()
    db.refresh(team)
    return TeamRead.model_validate(team)


@roster_router.get("/teams/{team_id}/players", response_model=List[PlayerBrief])
def list_team_players(team_id: int, include_inactive: bool = Query(False),
                      current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    if current_user.role == ROLE_PLAYER:
        raise HTTPException(status_code=403, detail="Player accounts cannot browse team rosters")
    ensure_team_access(current_user, team_id)
    query = db.query(Player).filter(Player.team_id == team_id)
    if not include_inactive:
        query = query.filter(Player.active.is_(True))
    return [PlayerBrief.model_validate(p) for p in query.order_by(Player.jersey_number, Player.id).all()]


# ---- players (write side) ----------------------------------------------------------------------
def _check_jersey_free(db: Session, team_id: Optional[int], jersey: Optional[int],
                       exclude_id: Optional[int] = None) -> None:
    if team_id is None or jersey is None:
        return
    query = db.query(Player.id).filter(Player.team_id == team_id, Player.jersey_number == jersey,
                                       Player.active.is_(True))
    if exclude_id is not None:
        query = query.filter(Player.id != exclude_id)
    if query.first() is not None:
        raise HTTPException(status_code=409, detail=f"Jersey {jersey} is already used by an active player in this team")


@roster_router.post("/players", response_model=PlayerRead, status_code=201)
def create_player(data: PlayerCreate, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    if data.team_id is not None and db.query(Team.id).filter(Team.id == data.team_id).first() is None:
        raise HTTPException(status_code=404, detail=f"Team {data.team_id} not found")
    if data.active:
        _check_jersey_free(db, data.team_id, data.jersey_number)
    player = Player(team_id=data.team_id, name=data.name.strip(), jersey_number=data.jersey_number,
                    position=data.position, active=data.active, reference_images=data.reference_images,
                    face_embedding=data.face_embedding)
    db.add(player)
    db.commit()
    return PlayerRead.model_validate(_player_or_404(db, player.id))


@roster_router.patch("/players/{player_id}", response_model=PlayerRead)
def update_player(player_id: int, data: PlayerUpdate, _: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    player = _player_or_404(db, player_id)
    changes = data.model_dump(exclude_unset=True)
    team_id = changes.get("team_id", player.team_id)
    jersey = changes.get("jersey_number", player.jersey_number)
    active = changes.get("active", player.active)
    if "team_id" in changes and team_id is not None and db.query(Team.id).filter(Team.id == team_id).first() is None:
        raise HTTPException(status_code=404, detail=f"Team {team_id} not found")
    if active:
        _check_jersey_free(db, team_id, jersey, exclude_id=player.id)
    for field in ("team_id", "name", "jersey_number", "position", "active", "reference_images", "face_embedding"):
        if field in changes:
            value = changes[field]
            setattr(player, field, value.strip() if field == "name" and value is not None else value)
    if player.user is not None:
        player.user.team_id = player.team_id
        if not player.active:
            player.user.active = False
    db.commit()
    return PlayerRead.model_validate(_player_or_404(db, player_id))


@roster_router.delete("/players/{player_id}")
def deactivate_player(player_id: int, current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Soft delete: history (events, tracks, collisions) stays intact; the AI can no longer attach this player."""
    player = _player_or_404(db, player_id)
    if player.active:
        player.active = False
        if player.user is not None:
            player.user.active = False
        db.commit()
        logger.info("player %s deactivated by %s", player.id, current_user.username)
    return {"message": "Player deactivated", "player_id": player.id}


# ===========================================================================
# VIDEOS ROUTER (metadata, validated browser upload, and processing state)
# ===========================================================================
videos_router = APIRouter(tags=["videos"])


def _video_or_404(db: Session, video_id: int) -> Video:
    video = db.query(Video).filter(Video.id == video_id).first()
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


@videos_router.post("/videos", response_model=VideoRead, status_code=201)
def create_video(
    data: VideoCreate,
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    _match_or_404(db, data.match_session_id)
    video = Video(**data.model_dump(exclude_unset=True))
    db.add(video)
    db.commit()
    db.refresh(video)
    logger.info("video created id=%s match=%s by=%s", video.id, video.match_session_id, principal.label)
    return VideoRead.model_validate(video)


@videos_router.post("/videos/upload", response_model=VideoRead, status_code=201)
def upload_match_video(
    match_session_id: int = Form(...),
    file: UploadFile = File(...),
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """Persist a validated browser upload and attach it to its real match row.

    Upload acceptance creates the persisted input record. Admins start processing
    from POST /matches/{match_id}/start; the worker then manages real status changes.
    """
    match = _match_or_404(db, match_session_id)
    if match.status in (MATCH_LIVE, MATCH_FINISHED):
        raise HTTPException(status_code=409, detail="A video cannot be uploaded to a match that is already processing or finished")

    supplied_name = (file.filename or "").replace("\\", "/").split("/")[-1].strip()
    suffix = Path(supplied_name).suffix.lower()
    allowed_extensions = tuple(ext.lower() for ext in settings.ALLOWED_VIDEO_EXTENSIONS)
    if not supplied_name or suffix not in allowed_extensions:
        raise HTTPException(status_code=415, detail="Unsupported video file extension")

    video_dir = Path(settings.VIDEOS_DIR).expanduser().resolve()
    try:
        video_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("video upload directory is unavailable")
        raise HTTPException(status_code=503, detail="Video storage is unavailable")

    stored_name = uuid.uuid4().hex + suffix
    temporary_path = video_dir / ("." + uuid.uuid4().hex + ".upload" + suffix)
    final_path = video_dir / stored_name
    max_bytes = int(settings.MAX_VIDEO_UPLOAD_MB) * 1024 * 1024
    size_bytes = 0
    checksum = hashlib.sha256()
    fps = width = height = frame_count = None
    duration = None

    try:
        with temporary_path.open("xb") as target:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > max_bytes:
                    raise HTTPException(status_code=413, detail="Video exceeds the configured upload size limit")
                checksum.update(chunk)
                target.write(chunk)
        if size_bytes == 0:
            raise HTTPException(status_code=422, detail="The uploaded video is empty")

        # Probe one decoded frame and basic metadata; do not run detection here.
        try:
            import cv2
            capture = cv2.VideoCapture(str(temporary_path))
            try:
                opened = capture.isOpened()
                readable, _frame = capture.read() if opened else (False, None)
                if not opened or not readable:
                    raise HTTPException(status_code=422, detail="The file is not a readable video")
                fps_value = float(capture.get(cv2.CAP_PROP_FPS))
                width_value = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
                height_value = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
                frame_value = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
                if fps_value > 0:
                    fps = fps_value
                    if frame_value >= 0:
                        duration = frame_value / fps_value
                width = width_value if width_value > 0 else None
                height = height_value if height_value > 0 else None
                frame_count = frame_value if frame_value >= 0 else None
            finally:
                capture.release()
        except HTTPException:
            raise
        except Exception as error:
            logger.info("video probe failed for upload %s: %s", stored_name, error)
            raise HTTPException(status_code=422, detail="The uploaded file could not be validated as a video")

        digest = checksum.hexdigest()
        duplicate = db.query(Video.id).filter(
            Video.match_session_id == match.id,
            Video.checksum_sha256 == digest,
        ).first()
        if duplicate:
            raise HTTPException(status_code=409, detail="This video is already attached to the match")

        temporary_path.replace(final_path)
        guessed_type = mimetypes.guess_type(supplied_name)[0]
        content_type = file.content_type if file.content_type and file.content_type.startswith("video/") else guessed_type
        video = Video(
            match_session_id=match.id,
            source_type="file",
            original_filename=supplied_name[:255],
            file_path=str(final_path),
            content_type=content_type,
            file_size_bytes=size_bytes,
            checksum_sha256=digest,
            fps=fps,
            width=width,
            height=height,
            total_frames=frame_count,
            duration_sec=duration,
            status=VIDEO_UPLOADED,
            uploaded_by_id=principal.user.id if principal.user is not None else None,
        )
        db.add(video)
        match.video_source = str(final_path)
        match.status = "VIDEO_UPLOADED"
        db.commit()
        db.refresh(video)
        logger.info("video upload accepted id=%s match=%s bytes=%s by=%s", video.id, match.id, size_bytes, principal.label)
        return VideoRead.model_validate(video)
    except IntegrityError:
        db.rollback()
        final_path.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail="This video conflicts with an existing upload for the match")
    except HTTPException:
        db.rollback()
        final_path.unlink(missing_ok=True)
        raise
    except OSError as error:
        db.rollback()
        final_path.unlink(missing_ok=True)
        temporary_path.unlink(missing_ok=True)
        logger.warning("video upload storage failed: %s", error)
        raise HTTPException(status_code=503, detail="Video could not be stored; retry the upload")
    except Exception:
        db.rollback()
        final_path.unlink(missing_ok=True)
        temporary_path.unlink(missing_ok=True)
        logger.exception("unexpected video upload failure for match %s", match_session_id)
        raise HTTPException(status_code=500, detail="Video upload failed")
    finally:
        file.file.close()
        temporary_path.unlink(missing_ok=True)


@videos_router.get("/videos", response_model=List[VideoRead])
def list_videos(
    match_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    query = db.query(Video)
    if current_user.role in STAFF_ROLES:
        pass
    elif current_user.role == ROLE_COACH:
        if current_user.team_id is None:
            query = query.filter(Video.id.in_([]))
        else:
            query = query.join(MatchSession, Video.match_session_id == MatchSession.id).filter(
                or_(MatchSession.team_a_id == current_user.team_id,
                    MatchSession.team_b_id == current_user.team_id))
    else:
        raise HTTPException(status_code=403, detail="You do not have permission to access videos")
    if match_id is not None:
        query = query.filter(Video.match_session_id == match_id)
    if status:
        query = query.filter(Video.status == status.strip().upper())
    rows = query.order_by(Video.id.desc()).offset(offset).limit(limit).all()
    return [VideoRead.model_validate(v) for v in rows]


@videos_router.get("/videos/{video_id}", response_model=VideoRead)
def get_video(video_id: int, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    if current_user.role == ROLE_PLAYER:
        raise HTTPException(status_code=403, detail="Player accounts cannot access video records")
    video = _video_or_404(db, video_id)
    if video.match_session_id is None:
        if current_user.role not in STAFF_ROLES:
            raise HTTPException(status_code=403, detail="You can only access videos from your own team's matches")
    else:
        _ensure_match_access(_match_or_404(db, video.match_session_id), current_user)
    return VideoRead.model_validate(video)


@videos_router.patch("/videos/{video_id}", response_model=VideoRead)
def update_video(
    video_id: int,
    data: VideoUpdate,
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """The AI process reports processing state here: UPLOADED -> PROCESSING -> PROCESSED / FAILED."""
    video = _video_or_404(db, video_id)
    changes = data.model_dump(exclude_unset=True)
    if "status" in changes:
        new_status = (changes["status"] or "").strip().upper()
        if new_status not in VIDEO_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {list(VIDEO_STATUSES)}")
        changes["status"] = new_status
    for field in ("status", "fps", "width", "height", "total_frames", "duration_sec",
                  "processed_at", "error_message"):
        if field in changes:
            setattr(video, field, changes[field])
    if "status" in changes:
        match = _match_or_404(db, video.match_session_id)
        match_status = {
            "UPLOADED": "VIDEO_UPLOADED",
            "PROCESSING": "PROCESSING",
            "PROCESSED": "COMPLETED",
            "FAILED": "FAILED",
        }[video.status]
        if video.status != "UPLOADED" or match.status in ("CREATED", "VIDEO_UPLOADED"):
            match.status = match_status
    db.commit()
    db.refresh(video)
    logger.info("video %s updated by %s (status=%s)", video.id, principal.label, video.status)
    return VideoRead.model_validate(video)


# ===========================================================================
# ALERTS ROUTER
# One alert per incident: callers (alert_system.py / the AI) pass a stable dedupe_key.
# ===========================================================================
alerts_router = APIRouter(tags=["alerts"])
_ALERT_STATUSES = tuple(_config.ALERT_STATUSES)


def _alert_scope(query, user: User):
    """Staff see every alert; a coach only sees alerts addressed to them or to their team."""
    if user.role in STAFF_ROLES:
        return query
    if user.role == ROLE_COACH:
        conditions = [Alert.recipient_user_id == user.id]
        if user.team_id is not None:
            conditions.append(Alert.team_id == user.team_id)
        return query.filter(or_(*conditions))
    if user.role == ROLE_PLAYER:
        if user.player_id is None:
            raise HTTPException(status_code=403, detail="This account has no player profile")
        return query.filter(or_(
            Alert.injury_event.has(InjuryEvent.player_id == user.player_id),
            Alert.collision_event.has(
                CollisionEvent.participants.any(CollisionParticipant.player_id == user.player_id)),
        ))
    raise HTTPException(status_code=403, detail="Unknown role")


def _visible_alert_or_404(db: Session, alert_id: int, user: User) -> Alert:
    alert = _alert_scope(db.query(Alert), user).filter(Alert.id == alert_id).first()
    if alert is None:   # same answer for "missing" and "not yours": no existence leak
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


@alerts_router.post("/alerts", response_model=AlertRead, status_code=201)
def create_alert(
    data: AlertCreate,
    response: Response,
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    if data.dedupe_key:
        existing = db.query(Alert).filter(Alert.dedupe_key == data.dedupe_key).first()
        if existing is not None:
            response.status_code = 200            # repeated detection of the same incident
            return AlertRead.model_validate(existing)
    if data.injury_event_id is None and data.collision_event_id is None:
        raise HTTPException(status_code=422, detail="An alert needs injury_event_id or collision_event_id")
    if data.injury_event_id is not None:
        _event_or_404(db, data.injury_event_id)
    if data.collision_event_id is not None:
        _collision_or_404(db, data.collision_event_id)
    if data.recipient_user_id is not None and db.query(User.id).filter(User.id == data.recipient_user_id).first() is None:
        raise HTTPException(status_code=404, detail="Recipient user not found")
    if data.team_id is not None and db.query(Team.id).filter(Team.id == data.team_id).first() is None:
        raise HTTPException(status_code=404, detail="Recipient team not found")
    severity = data.severity.strip().upper() if data.severity else None
    if severity is not None and severity not in RISK_ORDER:
        raise HTTPException(status_code=422, detail=f"severity must be one of {sorted(RISK_ORDER)}")

    alert = Alert(
        injury_event_id=data.injury_event_id, collision_event_id=data.collision_event_id,
        recipient_user_id=data.recipient_user_id, team_id=data.team_id,
        alert_type=data.alert_type.strip().upper() if data.alert_type else "NEW_EVENT", severity=severity,
        channel=data.channel.strip().lower() if data.channel else "web",
        title=data.title, message=data.message, status=ALERT_STATUS_PENDING, dedupe_key=data.dedupe_key,
    )
    db.add(alert)
    try:
        db.commit()
    except IntegrityError:                         # two callers raced on the same dedupe_key
        db.rollback()
        existing = db.query(Alert).filter(Alert.dedupe_key == data.dedupe_key).first() if data.dedupe_key else None
        if existing is None:
            raise
        response.status_code = 200
        return AlertRead.model_validate(existing)
    db.refresh(alert)
    logger.info("alert created id=%s injury=%s collision=%s by=%s", alert.id, alert.injury_event_id,
                alert.collision_event_id, principal.label)
    return AlertRead.model_validate(alert)


@alerts_router.get("/alerts", response_model=List[AlertRead])
def list_alerts(
    unread: Optional[bool] = Query(None, description="true = active (unread) alerts only"),
    match_id: Optional[int] = Query(None),
    severity: Optional[str] = Query(None),
    alert_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Newest first. Active = unread=true, recent = default limit, history = page with offset."""
    query = _alert_scope(db.query(Alert), current_user)
    if unread is not None:
        query = query.filter(Alert.is_read.is_(not unread))
    if match_id is not None:
        query = query.filter(or_(Alert.injury_event.has(InjuryEvent.match_session_id == match_id),
                                 Alert.collision_event.has(CollisionEvent.match_session_id == match_id)))
    if severity:
        query = query.filter(Alert.severity == severity.strip().upper())
    if alert_type:
        query = query.filter(Alert.alert_type == alert_type.strip().upper())
    rows = query.order_by(Alert.created_at.desc(), Alert.id.desc()).offset(offset).limit(limit).all()
    return [AlertRead.model_validate(a) for a in rows]


@alerts_router.patch("/alerts/{alert_id}/read", response_model=AlertRead)
def mark_alert_read(alert_id: int, data: AlertMarkRead, current_user: User = Depends(get_current_active_user),
                    db: Session = Depends(get_db)):
    if data.alert_ids is not None and alert_id not in data.alert_ids:
        raise HTTPException(status_code=422, detail="alert_ids must include the alert in the URL")
    alert = _visible_alert_or_404(db, alert_id, current_user)
    alert.is_read = True
    alert.read_at = _now()
    db.commit()
    db.refresh(alert)
    return AlertRead.model_validate(alert)


@alerts_router.patch("/alerts/{alert_id}/status", response_model=AlertRead)
def update_alert_status(
    alert_id: int,
    data: AlertStatusUpdate,
    principal: Principal = Depends(require_service_or_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """Delivery state reported by the notifier (e.g. the WhatsApp sender): PENDING / SENT / FAILED / READ."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    new_status = data.status.strip().upper()
    if new_status not in _ALERT_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {list(_ALERT_STATUSES)}")
    alert.status = new_status
    alert.error_message = data.error_message if new_status == ALERT_STATUS_FAILED else None
    if new_status == ALERT_STATUS_SENT and alert.sent_at is None:
        alert.sent_at = _now()
    db.commit()
    db.refresh(alert)
    logger.info("alert %s status=%s by=%s", alert.id, alert.status, principal.label)
    return AlertRead.model_validate(alert)


# ===========================================================================
# MEDIA ROUTER - signed clip delivery (replaces the open static mount)
# ===========================================================================
media_router = APIRouter(tags=["media"])


@media_router.get("/clips/{clip_path:path}")
def serve_clip(clip_path: str, exp: Optional[int] = Query(None), sig: Optional[str] = Query(None)):
    normalised = _normalise_clip_path(clip_path)
    if settings.CLIPS_REQUIRE_SIGNATURE:
        valid = (
            exp is not None and sig is not None and exp >= int(time.time())
            and hmac.compare_digest(sig.encode("utf-8"), _clip_signature(normalised, exp).encode("utf-8"))
        )
        if not valid:
            raise HTTPException(status_code=403, detail="Invalid or expired clip link")
    root = Path(settings.CLIPS_DIR).resolve()
    target = (root / normalised).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="Clip not found")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})


# ===========================================================================
# SYSTEM ROUTER
# ===========================================================================
system_router = APIRouter(tags=["system"])


@system_router.get("/")
def root():
    return {"name": settings.APP_NAME, "version": settings.APP_VERSION,
            "docs": None if settings.is_production else "/docs", "health": "/health"}


@system_router.get("/health", response_model=HealthRead)
def health(response: Response):
    database_ok = check_connection() and bool(getattr(app.state, "db_ready", False))
    if not database_ok:
        response.status_code = 503
    return HealthRead(status="ok" if database_ok else "degraded", database="up" if database_ok else "down",
                      version=settings.APP_VERSION, environment=settings.ENVIRONMENT)


# ===========================================================================
# Register routers (each URL is declared exactly once)
# ===========================================================================
for _router in (system_router, auth_router, users_router, matches_router, events_router, collisions_router,
                tracks_router, roster_router, videos_router, alerts_router, media_router):
    app.include_router(_router)


if __name__ == "__main__":  # optional: `python main.py` (same as the uvicorn command below)
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=not settings.is_production)
