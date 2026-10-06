import os
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import or_, text
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from database import get_db, SessionLocal, Base
from models import User, Player, InjuryEvent, Team, MatchSession
from auth import verify_password, create_access_token, decode_access_token


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIPS_DIR = os.path.join(PROJECT_ROOT, "output", "clips")
os.makedirs(CLIPS_DIR, exist_ok=True)

app.mount("/clips", StaticFiles(directory=CLIPS_DIR), name="clips")


# ============================================
# STARTUP BOOTSTRAP (replaces create_tables, migrate_v2,
# seed_match_2006 and add_players). It never creates or changes accounts.
# ============================================

MIGRATIONS = [
    "ALTER TABLE match_sessions ADD COLUMN IF NOT EXISTS name VARCHAR",
    "ALTER TABLE match_sessions ADD COLUMN IF NOT EXISTS video_source VARCHAR",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS region VARCHAR",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS risk VARCHAR",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS movement DOUBLE PRECISION",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS note VARCHAR",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS source VARCHAR NOT NULL DEFAULT 'ai'",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS resolved BOOLEAN DEFAULT FALSE",
    'ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS "timestamp" TIMESTAMPTZ DEFAULT now()',
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS match_session_id INTEGER REFERENCES match_sessions(id)",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS injury_note VARCHAR",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS clip_path VARCHAR",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS identified_at TIMESTAMPTZ",
    "ALTER TABLE injury_events ALTER COLUMN player_id DROP NOT NULL",
    # v3 — how and where the player was identified
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS track_id INTEGER",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS video_time_sec DOUBLE PRECISION",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS identified_by VARCHAR",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS id_detail VARCHAR",
    "ALTER TABLE injury_events ADD COLUMN IF NOT EXISTS id_confidence DOUBLE PRECISION",
]

ROSTERS = {
    "Portugal": [
        (1, "Ricardo"),
        (5, "Fernando Meira"),
        (6, "Costinha"),
        (7, "Lu\u00eds Figo"),
        (8, "Petit"),
        (9, "Pauleta"),
        (11, "Sim\u00e3o"),
        (13, "Miguel"),
        (14, "Nuno Valente"),
        (16, "Ricardo Carvalho"),
        (17, "Cristiano Ronaldo"),
        (18, "Maniche"),
        (19, "Tiago"),
        (20, "Deco"),
    ],
    "Netherlands": [
        (1, "Edwin van der Sar"),
        (3, "Khalid Boulahrouz"),
        (4, "Joris Mathijsen"),
        (5, "Giovanni van Bronckhorst"),
        (7, "Dirk Kuyt"),
        (8, "Phillip Cocu"),
        (10, "Rafael van der Vaart"),
        (11, "Arjen Robben"),
        (13, "Andr\u00e9 Ooijer"),
        (14, "John Heitinga"),
        (17, "Robin van Persie"),
        (18, "Mark van Bommel"),
        (19, "Jan Vennegoor of Hesselink"),
        (20, "Wesley Sneijder"),
    ],
}

PLACEHOLDER_TEAMS = {"Portugal": "Team A", "Netherlands": "Team B"}


def bootstrap_data():

    probe = SessionLocal()
    engine = probe.get_bind()
    probe.close()

    # 1. tables + columns
    Base.metadata.create_all(bind=engine)

    for sql in MIGRATIONS:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except Exception as e:
            print("[BOOTSTRAP] migration skipped:", sql[:60], "|", e)

    db = SessionLocal()

    try:
        # 2. teams + players
        teams = {}

        for team_name, roster in ROSTERS.items():

            team = db.query(Team).filter(Team.name == team_name).first()

            if team is None:
                placeholder = db.query(Team).filter(Team.name == PLACEHOLDER_TEAMS[team_name]).first()
                if placeholder is not None:
                    placeholder.name = team_name
                    team = placeholder

            if team is None:
                team = Team(name=team_name)
                db.add(team)
                db.flush()

            teams[team_name] = team

            existing = {}
            for p in db.query(Player).filter(Player.team_id == team.id).all():
                existing.setdefault(p.jersey_number, p)

            for number, name in roster:
                if number not in existing:
                    db.add(Player(team_id=team.id, name=name, jersey_number=number))
                    print("[BOOTSTRAP] added player:", team_name, "#" + str(number), name)

        db.flush()

        db.commit()

        print("[BOOTSTRAP] done:", db.query(Player).count(), "players,", db.query(User).count(), "users")

    except Exception as e:
        db.rollback()
        print("[BOOTSTRAP] failed:", e)

    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    bootstrap_data()


# ============================================
# REQUEST SCHEMAS
# ============================================

class LoginRequest(BaseModel):
    username: str
    password: str


class EventCreateRequest(BaseModel):
    player_id: Optional[int] = None          # None = Unidentified
    match_session_id: Optional[int] = None
    event_type: str
    region: Optional[str] = None
    risk: Optional[str] = None
    movement: Optional[float] = None
    injury_note: Optional[str] = None
    clip_path: Optional[str] = None
    source: str = "ai"
    track_id: Optional[int] = None
    video_time_sec: Optional[float] = None
    identified_by: Optional[str] = None      # jersey / jersey_name / jersey_history / jersey_handoff / face
    id_detail: Optional[str] = None          # e.g. "jersey #17", "face match (3 frames)"
    id_confidence: Optional[float] = None    # 0..1


class EventAIUpdateRequest(BaseModel):
    player_id: Optional[int] = None
    clip_path: Optional[str] = None
    injury_note: Optional[str] = None
    identified_by: Optional[str] = None
    id_detail: Optional[str] = None
    id_confidence: Optional[float] = None


class AssignPlayerRequest(BaseModel):
    player_id: int


class ManualEventRequest(BaseModel):
    player_id: int
    event_type: str
    note: str
    risk: Optional[str] = "MEDIUM"
    match_session_id: Optional[int] = None


class MatchCreateRequest(BaseModel):
    name: Optional[str] = None
    video_source: Optional[str] = None


# ============================================
# AUTH DEPENDENCY
# ============================================

def get_current_user(authorization: str = Header(...), db: Session = Depends(get_db)):

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.replace("Bearer ", "")

    payload = decode_access_token(token)

    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = db.query(User).filter(User.id == payload.get("user_id")).first()

    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    return user


# ============================================
# SERIALIZER
# ============================================

def event_to_dict(e: InjuryEvent):

    return {
        "id": e.id,
        "player_id": e.player_id,
        "player_name": e.player.name if e.player is not None else "Unidentified",
        "jersey_number": e.player.jersey_number if e.player is not None else None,
        "team_name": (e.player.team.name if e.player is not None and e.player.team is not None else None),
        "identified": e.player_id is not None,
        "identified_at": e.identified_at,
        "identified_by": e.identified_by,
        "id_detail": e.id_detail,
        "id_confidence": e.id_confidence,
        "track_id": e.track_id,
        "video_time_sec": e.video_time_sec,
        "match_session_id": e.match_session_id,
        "match_name": (e.match.name if e.match is not None and e.match.name else
                       ("Match #" + str(e.match_session_id) if e.match_session_id else None)),
        "event_type": e.event_type,
        "region": e.region,
        "risk": e.risk,
        "movement": e.movement,
        "note": e.note,
        "injury_note": e.injury_note,
        "clip_url": ("/clips/" + e.clip_path) if e.clip_path else None,
        "source": e.source,
        "resolved": e.resolved,
        "timestamp": e.timestamp,
    }


# ============================================
# LOGIN
# ============================================

@app.post("/auth/login")
def login(request: LoginRequest, db: Session = Depends(get_db)):

    user = db.query(User).filter(User.username == request.username).first()

    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = create_access_token({
        "user_id": user.id,
        "role": user.role,
        "team_id": user.team_id
    })

    return {
        "access_token": token,
        "role": user.role,
        "team_id": user.team_id,
        "username": user.username
    }


# ============================================
# MATCHES
# ============================================

@app.post("/matches")
def create_match(request: MatchCreateRequest, db: Session = Depends(get_db)):

    match = MatchSession(name=request.name, video_source=request.video_source)

    db.add(match)
    db.commit()
    db.refresh(match)

    return {"message": "Match created", "match_session_id": match.id}


@app.get("/matches")
def get_matches(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):

    matches = db.query(MatchSession).order_by(MatchSession.date.desc()).all()

    return [
        {
            "id": m.id,
            "name": m.name or ("Match #" + str(m.id)),
            "video_source": m.video_source,
            "date": m.date,
        }
        for m in matches
    ]


# ============================================
# EVENTS — AI SCRIPT POSTS HERE (saved even without a player)
# ============================================

@app.post("/events")
def create_event(request: EventCreateRequest, db: Session = Depends(get_db)):

    if request.player_id is not None:
        player = db.query(Player).filter(Player.id == request.player_id).first()
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

    if request.match_session_id is not None:
        match = db.query(MatchSession).filter(MatchSession.id == request.match_session_id).first()
        if match is None:
            raise HTTPException(status_code=404, detail="Match session not found")

    event = InjuryEvent(
        player_id=request.player_id,
        match_session_id=request.match_session_id,
        event_type=request.event_type,
        region=request.region,
        risk=request.risk,
        movement=request.movement,
        injury_note=request.injury_note,
        clip_path=request.clip_path,
        source=request.source,
        track_id=request.track_id,
        video_time_sec=request.video_time_sec,
    )

    if request.player_id is not None:
        event.identified_at = func.now()
        event.identified_by = request.identified_by
        event.id_detail = request.id_detail
        event.id_confidence = request.id_confidence

    db.add(event)
    db.commit()
    db.refresh(event)

    return {"message": "Event recorded", "event_id": event.id}


# ============================================
# EVENTS — AI SCRIPT FILLS IN PLAYER / CLIP LATER
# ============================================

@app.patch("/events/{event_id}/ai-update")
def ai_update_event(event_id: int, request: EventAIUpdateRequest, db: Session = Depends(get_db)):

    event = db.query(InjuryEvent).filter(InjuryEvent.id == event_id).first()

    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")

    if request.player_id is not None:

        player = db.query(Player).filter(Player.id == request.player_id).first()

        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        event.player_id = request.player_id
        event.identified_at = func.now()
        event.identified_by = request.identified_by
        event.id_detail = request.id_detail
        event.id_confidence = request.id_confidence

    if request.clip_path is not None:
        event.clip_path = request.clip_path

    if request.injury_note is not None:
        event.injury_note = request.injury_note

    db.commit()
    db.refresh(event)

    return {"message": "Event updated", "event": event_to_dict(event)}


# ============================================
# EVENTS — STAFF ASSIGN / CORRECT THE PLAYER
# Used from the dashboard after watching the clip.
# Works for Unidentified events and to fix a wrong AI match.
# ============================================

@app.patch("/events/{event_id}/assign")
def assign_player(
    event_id: int,
    request: AssignPlayerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    if current_user.role not in ("medical", "admin"):
        raise HTTPException(status_code=403, detail="Only medical staff or admin can assign players")

    event = db.query(InjuryEvent).filter(InjuryEvent.id == event_id).first()

    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")

    player = db.query(Player).filter(Player.id == request.player_id).first()

    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    previous = event.player.name if event.player is not None else "Unidentified"

    event.player_id = player.id
    event.identified_at = func.now()
    event.identified_by = "manual"
    event.id_detail = "assigned by " + current_user.username + " (was: " + previous + ")"
    event.id_confidence = None

    db.commit()
    db.refresh(event)

    return {"message": "Player assigned", "event": event_to_dict(event)}


# ============================================
# EVENTS — MEDICAL STAFF MANUAL LOGGING
# ============================================

@app.post("/events/manual")
def create_manual_event(
    request: ManualEventRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    if current_user.role != "medical":
        raise HTTPException(status_code=403, detail="Only medical staff can log manual events")

    player = db.query(Player).filter(Player.id == request.player_id).first()

    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    event = InjuryEvent(
        player_id=request.player_id,
        match_session_id=request.match_session_id,
        event_type=request.event_type,
        note=request.note,
        risk=request.risk,
        source="manual",
        identified_by="manual",
        id_detail="logged by " + current_user.username,
    )

    event.identified_at = func.now()

    db.add(event)
    db.commit()
    db.refresh(event)

    return {"message": "Manual event recorded", "event_id": event.id}


# ============================================
# GET EVENTS — medical/admin: all. coach: own team + Unidentified
# ============================================

@app.get("/events")
def get_events(
    match_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    query = db.query(InjuryEvent).outerjoin(Player, InjuryEvent.player_id == Player.id)

    if current_user.role in ("medical", "admin"):
        pass

    elif current_user.role == "coach":
        query = query.filter(
            or_(
                Player.team_id == current_user.team_id,
                InjuryEvent.player_id.is_(None)
            )
        )

    else:
        raise HTTPException(status_code=403, detail="Unknown role")

    if match_id is not None:
        query = query.filter(InjuryEvent.match_session_id == match_id)

    events = query.order_by(InjuryEvent.timestamp.desc()).all()

    return [event_to_dict(e) for e in events]


# ============================================
# GET PLAYERS
# ============================================

@app.get("/players")
def get_players(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):

    if current_user.role in ("medical", "admin"):
        players = db.query(Player).all()
    else:
        players = db.query(Player).filter(Player.team_id == current_user.team_id).all()

    return [
        {"id": p.id, "name": p.name, "jersey_number": p.jersey_number, "team_id": p.team_id}
        for p in players
    ]


# ============================================
# GET TEAMS — admin/medical: both teams. coach: own team only.
# ============================================

@app.get("/teams")
def get_teams(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):

    if current_user.role in ("medical", "admin"):
        teams = db.query(Team).order_by(Team.id).all()

    elif current_user.role == "coach":
        teams = db.query(Team).filter(Team.id == current_user.team_id).all()

    else:
        raise HTTPException(status_code=403, detail="Unknown role")

    result = []

    for t in teams:

        players = (
            db.query(Player)
            .filter(Player.team_id == t.id)
            .order_by(Player.jersey_number)
            .all()
        )

        coaches = db.query(User).filter(User.role == "coach", User.team_id == t.id).all()

        rows = []
        team_total = 0
        team_high = 0

        for p in players:

            total = (
                db.query(func.count(InjuryEvent.id))
                .filter(InjuryEvent.player_id == p.id)
                .scalar()
            ) or 0

            high = (
                db.query(func.count(InjuryEvent.id))
                .filter(InjuryEvent.player_id == p.id, InjuryEvent.risk == "HIGH")
                .scalar()
            ) or 0

            team_total += total
            team_high += high

            rows.append({
                "id": p.id,
                "name": p.name,
                "jersey_number": p.jersey_number,
                "injury_events": total,
                "high_risk_events": high,
            })

        result.append({
            "id": t.id,
            "name": t.name,
            "coaches": [c.username for c in coaches],
            "player_count": len(rows),
            "injury_events": team_total,
            "high_risk_events": team_high,
            "players": rows,
        })

    return result


# ============================================
# MARK EVENT RESOLVED
# ============================================

@app.patch("/events/{event_id}/resolve")
def resolve_event(event_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):

    event = db.query(InjuryEvent).filter(InjuryEvent.id == event_id).first()

    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")

    event.resolved = True
    db.commit()

    return {"message": "Event marked as resolved"}


# ============================================
# ADMIN — CLEAR ALL INJURY EVENTS (players are kept)
# ============================================

@app.delete("/admin/players/clear")
def clear_all_injury_events(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):

    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admin can perform this action")

    deleted = db.query(InjuryEvent).delete()
    db.commit()

    return {"message": "All injury events deleted. Players were kept.", "deleted_events": deleted}