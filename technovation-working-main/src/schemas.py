"""
AthleteGuard - Pydantic schemas (Pydantic v2, FastAPI request / response models).

Compatibility with the existing main.py:
    LoginRequest, EventCreateRequest, EventAIUpdateRequest, AssignPlayerRequest,
    ManualEventRequest and MatchCreateRequest keep their existing names and
    fields (new fields are optional additions). InjuryEventRead.from_event()
    returns exactly the keys main.py's event_to_dict() returns, plus new ones.

Identity rule: every schema keeps `track_id` (tracker number, per video run)
and `player_id` (players.id in the database) as two separate fields.

Collision rule: a collision always has exactly two participants, two different
tracker ids, two different players (when known) and the slots 1 and 2.

This file only imports constants from config.py. It never imports models.py,
so ORM objects are read through `from_attributes`.
"""

import re
from datetime import datetime
from typing import Annotated, Any, Dict, List, Optional, Tuple

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)

from config import (
    ALERT_CHANNELS,
    ALERT_STATUSES,
    ALERT_TYPES,
    COLLISION_PARTICIPANT_SLOTS,
    EVENT_SOURCES,
    EVENT_TYPE_COLLISION,
    EVENT_TYPE_FALL,
    IDENTITY_STATUSES,
    MATCH_STATUSES,
    RISK_LEVELS,
    ROLES,
    VIDEO_SOURCE_TYPES,
    VIDEO_STATUSES,
)

CLIPS_URL_PREFIX = "/clips/"

# Event types the AI pipeline is allowed to write (manual events may use any valid name)
AI_EVENT_TYPES: Tuple[str, ...] = (EVENT_TYPE_FALL, EVENT_TYPE_COLLISION)

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EVENT_TYPE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_]*$")


# ============================================
# REUSABLE FIELD TYPES
# ============================================

def _choice(allowed, transform):

    def check(value):
        cleaned = transform(str(value).strip())
        if cleaned not in allowed:
            raise ValueError("must be one of: " + ", ".join(str(a) for a in allowed))
        return cleaned

    return check


def _check_email(value):
    cleaned = str(value).strip().lower()
    if not _EMAIL_PATTERN.match(cleaned):
        raise ValueError("not a valid email address")
    return cleaned


def _normalize_event_type(value):
    # "twisted ankle" / "Twisted-Ankle" / "twisted_ankle" -> "TWISTED_ANKLE"
    cleaned = re.sub(r"[\s\-]+", "_", str(value).strip().upper()).strip("_")
    if not _EVENT_TYPE_PATTERN.match(cleaned):
        raise ValueError("event_type may only contain letters, digits and underscores (e.g. FALL, TWISTED_ANKLE)")
    return cleaned


RiskLevel = Annotated[str, AfterValidator(_choice(RISK_LEVELS, str.upper))]
RoleName = Annotated[str, AfterValidator(_choice(ROLES, str.lower))]
EventSource = Annotated[str, AfterValidator(_choice(EVENT_SOURCES, str.lower))]
IdentityStatus = Annotated[str, AfterValidator(_choice(IDENTITY_STATUSES, str.upper))]
MatchStatus = Annotated[str, AfterValidator(_choice(MATCH_STATUSES, str.upper))]
VideoStatus = Annotated[str, AfterValidator(_choice(VIDEO_STATUSES, str.upper))]
VideoSourceType = Annotated[str, AfterValidator(_choice(VIDEO_SOURCE_TYPES, str.lower))]
AlertType = Annotated[str, AfterValidator(_choice(ALERT_TYPES, str.upper))]
AlertChannel = Annotated[str, AfterValidator(_choice(ALERT_CHANNELS, str.lower))]
AlertStatus = Annotated[str, AfterValidator(_choice(ALERT_STATUSES, str.upper))]

EmailAddress = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=3, max_length=255),
    AfterValidator(_check_email),
]
EventTypeText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=50),
    AfterValidator(_normalize_event_type),
]
IdentifiedByText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]
# An empty dedupe key would collide in the unique (match, dedupe_key) index, so it is rejected.
DedupeKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]           # 0..1
NormalizedCoord = Annotated[float, Field(ge=0.0, le=1.0)]      # position in the frame, 0..1
TrackId = Annotated[int, Field(ge=0)]                          # tracker number, NOT a player id
DatabaseId = Annotated[int, Field(ge=1)]                       # a primary key


class ORMModel(BaseModel):
    """Base for response models that are built from SQLAlchemy objects."""
    model_config = ConfigDict(from_attributes=True)


class MessageResponse(BaseModel):
    message: str


# ============================================
# AUTH
# ============================================

class LoginRequest(BaseModel):
    username: str
    password: str


def _validate_password_bytes(value: str) -> str:
    if len(value.encode("utf-8")) > 72:
        raise ValueError("Password must be at most 72 UTF-8 bytes")
    return value


PasswordValue = Annotated[
    str,
    StringConstraints(min_length=8, max_length=128),
    AfterValidator(_validate_password_bytes),
]


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: PasswordValue


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    team_id: Optional[int] = None
    username: str


# ============================================
# USERS
# ============================================

class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: PasswordValue
    role: RoleName
    team_id: Optional[DatabaseId] = None
    player_id: Optional[DatabaseId] = None
    full_name: Optional[str] = Field(default=None, max_length=120)
    email: Optional[EmailAddress] = None
    phone: Optional[str] = Field(default=None, max_length=32)
    active: bool = True

    @model_validator(mode="after")
    def coach_needs_team(self):
        if self.role == "coach" and self.team_id is None:
            raise ValueError("a coach must be assigned to a team")
        if self.role == "player" and self.player_id is None:
            raise ValueError("a player account must be linked to a player profile")
        if self.role != "player" and self.player_id is not None:
            raise ValueError("only a player account can be linked to a player profile")
        return self


class UserUpdate(BaseModel):
    role: Optional[RoleName] = None
    team_id: Optional[DatabaseId] = None
    player_id: Optional[DatabaseId] = None
    full_name: Optional[str] = Field(default=None, max_length=120)
    email: Optional[EmailAddress] = None
    phone: Optional[str] = Field(default=None, max_length=32)
    active: Optional[bool] = None
    password: Optional[PasswordValue] = None


class UserRead(ORMModel):
    id: int
    username: str
    role: str
    team_id: Optional[int] = None
    player_id: Optional[int] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    active: bool = True
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


# ============================================
# TEAMS
# ============================================

class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    short_name: Optional[str] = Field(default=None, max_length=20)
    active: bool = True


class TeamUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    short_name: Optional[str] = Field(default=None, max_length=20)
    active: Optional[bool] = None


class TeamRead(ORMModel):
    id: int
    name: str
    short_name: Optional[str] = None
    active: bool = True
    created_at: Optional[datetime] = None


# ============================================
# PLAYERS
# ============================================

class PlayerCreate(BaseModel):
    team_id: DatabaseId
    name: str = Field(min_length=1, max_length=120)
    jersey_number: int = Field(ge=0, le=999)
    position: Optional[str] = Field(default=None, max_length=50)
    active: bool = True
    reference_images: Optional[List[str]] = None
    face_embedding: Optional[List[float]] = None


class PlayerUpdate(BaseModel):
    team_id: Optional[DatabaseId] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    jersey_number: Optional[int] = Field(default=None, ge=0, le=999)
    position: Optional[str] = Field(default=None, max_length=50)
    active: Optional[bool] = None
    reference_images: Optional[List[str]] = None
    face_embedding: Optional[List[float]] = None


class PlayerBrief(ORMModel):
    """Small player card used inside events, tracks and collisions."""
    id: int
    name: str
    jersey_number: int
    team_id: int
    team_name: Optional[str] = None


class PlayerRead(ORMModel):
    id: int
    team_id: int
    name: str
    jersey_number: int
    team_name: Optional[str] = None
    position: Optional[str] = None
    active: bool = True
    reference_images: Optional[List[str]] = None
    created_at: Optional[datetime] = None


# Shape returned by the existing GET /teams endpoint
class PlayerStatsRead(BaseModel):
    id: int
    name: str
    jersey_number: int
    injury_events: int = 0
    high_risk_events: int = 0


class TeamSummaryRead(BaseModel):
    id: int
    name: str
    coaches: List[str] = Field(default_factory=list)
    player_count: int = 0
    injury_events: int = 0
    high_risk_events: int = 0
    players: List[PlayerStatsRead] = Field(default_factory=list)


# ============================================
# MATCHES
# ============================================

class MatchCreateRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    video_source: Optional[str] = None
    team_a_id: Optional[DatabaseId] = None
    team_b_id: Optional[DatabaseId] = None
    date: Optional[datetime] = None

    @model_validator(mode="after")
    def teams_must_differ(self):
        if self.team_a_id is not None and self.team_a_id == self.team_b_id:
            raise ValueError("team_a_id and team_b_id must be different teams")
        return self


class MatchUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    video_source: Optional[str] = None
    team_a_id: Optional[DatabaseId] = None
    team_b_id: Optional[DatabaseId] = None
    date: Optional[datetime] = None
    status: Optional[MatchStatus] = None

    @model_validator(mode="after")
    def teams_must_differ(self):
        if self.team_a_id is not None and self.team_a_id == self.team_b_id:
            raise ValueError("team_a_id and team_b_id must be different teams")
        return self


class MatchRead(ORMModel):
    id: int
    name: Optional[str] = None
    video_source: Optional[str] = None
    date: Optional[datetime] = None
    team_a_id: Optional[int] = None
    team_b_id: Optional[int] = None
    status: str = "CREATED"
    created_at: Optional[datetime] = None

    @computed_field
    @property
    def display_name(self) -> str:
        return self.name or ("Match #" + str(self.id))


# ============================================
# VIDEOS
# ============================================

class VideoCreate(BaseModel):
    match_session_id: DatabaseId
    source_type: VideoSourceType = "file"
    original_filename: Optional[str] = Field(default=None, max_length=255)
    file_path: Optional[str] = Field(default=None, max_length=500)
    content_type: Optional[str] = Field(default=None, max_length=100)
    file_size_bytes: Optional[int] = Field(default=None, ge=0)
    checksum_sha256: Optional[str] = Field(default=None, min_length=64, max_length=64)
    fps: Optional[float] = Field(default=None, gt=0)
    width: Optional[int] = Field(default=None, gt=0)
    height: Optional[int] = Field(default=None, gt=0)
    total_frames: Optional[int] = Field(default=None, ge=0)
    duration_sec: Optional[float] = Field(default=None, ge=0)


class VideoUpdate(BaseModel):
    status: Optional[VideoStatus] = None
    fps: Optional[float] = Field(default=None, gt=0)
    width: Optional[int] = Field(default=None, gt=0)
    height: Optional[int] = Field(default=None, gt=0)
    total_frames: Optional[int] = Field(default=None, ge=0)
    duration_sec: Optional[float] = Field(default=None, ge=0)
    processed_at: Optional[datetime] = None
    error_message: Optional[str] = None


class VideoRead(ORMModel):
    id: int
    match_session_id: int
    source_type: str
    original_filename: Optional[str] = None
    file_path: Optional[str] = None
    content_type: Optional[str] = None
    file_size_bytes: Optional[int] = None
    checksum_sha256: Optional[str] = None
    fps: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    total_frames: Optional[int] = None
    duration_sec: Optional[float] = None
    status: str
    processed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None


# ============================================
# PLAYER TRACKS  (tracker id  <->  database player)
# ============================================

class PlayerTrackUpsert(BaseModel):
    """
    Create-or-update the identity of one tracker id inside one match.
    `track_id` is the tracker's number. `player_id` is players.id.
    `predecessor_track_id` is the tracker id this one took over from.
    """
    match_session_id: DatabaseId
    track_id: TrackId
    video_id: Optional[DatabaseId] = None
    player_id: Optional[DatabaseId] = None
    team_id: Optional[DatabaseId] = None
    identity_status: Optional[IdentityStatus] = None
    identified_by: Optional[IdentifiedByText] = None
    id_detail: Optional[str] = None
    id_confidence: Optional[Confidence] = None
    predecessor_track_id: Optional[TrackId] = None
    first_frame: Optional[int] = Field(default=None, ge=0)
    last_frame: Optional[int] = Field(default=None, ge=0)
    first_seen_sec: Optional[float] = Field(default=None, ge=0)
    last_seen_sec: Optional[float] = Field(default=None, ge=0)
    tracking_confidence: Optional[Confidence] = None

    @model_validator(mode="after")
    def check_identity(self):
        if self.identity_status in ("CONFIRMED", "UNCERTAIN") and self.player_id is None:
            raise ValueError("identity_status CONFIRMED/UNCERTAIN needs a player_id")
        if self.identity_status == "UNKNOWN" and self.player_id is not None:
            raise ValueError("identity_status UNKNOWN cannot have a player_id")
        if self.predecessor_track_id is not None and self.predecessor_track_id == self.track_id:
            raise ValueError("a track cannot be its own predecessor")
        if (
            self.first_frame is not None
            and self.last_frame is not None
            and self.last_frame < self.first_frame
        ):
            raise ValueError("last_frame must not be before first_frame")
        if (
            self.first_seen_sec is not None
            and self.last_seen_sec is not None
            and self.last_seen_sec < self.first_seen_sec
        ):
            raise ValueError("last_seen_sec must not be before first_seen_sec")
        return self


class PlayerTrackRead(ORMModel):
    id: int
    match_session_id: int
    video_id: Optional[int] = None
    track_id: int
    player_id: Optional[int] = None
    team_id: Optional[int] = None
    identity_status: str
    identified_by: Optional[str] = None
    id_detail: Optional[str] = None
    id_confidence: Optional[float] = None
    identified_at: Optional[datetime] = None
    predecessor_id: Optional[int] = None
    first_frame: Optional[int] = None
    last_frame: Optional[int] = None
    first_seen_sec: Optional[float] = None
    last_seen_sec: Optional[float] = None
    tracking_confidence: Optional[float] = None
    player: Optional[PlayerBrief] = None


# ============================================
# FALL / INJURY EVENTS  (existing endpoints stay compatible)
# ============================================

class EventCreateRequest(BaseModel):
    player_id: Optional[int] = None          # None = Unidentified
    match_session_id: Optional[int] = None
    event_type: EventTypeText
    region: Optional[str] = None
    risk: Optional[RiskLevel] = None
    movement: Optional[float] = None
    injury_note: Optional[str] = None
    clip_path: Optional[str] = None
    source: EventSource = "ai"
    track_id: Optional[TrackId] = None       # tracker number - never a player id
    video_time_sec: Optional[float] = Field(default=None, ge=0)
    identified_by: Optional[IdentifiedByText] = None      # jersey / jersey_name / jersey_history / jersey_handoff / face
    id_detail: Optional[str] = None                       # e.g. "jersey #17"
    id_confidence: Optional[Confidence] = None            # identity confidence, 0..1

    # ---- additions (all optional) ----
    video_id: Optional[DatabaseId] = None
    player_track_id: Optional[DatabaseId] = None
    collision_event_id: Optional[DatabaseId] = None       # the collision this fall/injury followed
    side: Optional[str] = Field(default=None, max_length=10)
    confidence: Optional[Confidence] = None               # event confidence, separate from id_confidence
    risk_score: Optional[float] = None
    frame_number: Optional[int] = Field(default=None, ge=0)
    location_x: Optional[NormalizedCoord] = None
    location_y: Optional[NormalizedCoord] = None
    dedupe_key: Optional[DedupeKey] = None

    @model_validator(mode="after")
    def check_event(self):
        if self.source == "ai" and self.event_type not in AI_EVENT_TYPES:
            raise ValueError("AI events must have event_type " + " or ".join(AI_EVENT_TYPES))
        if (self.location_x is None) != (self.location_y is None):
            raise ValueError("give both location_x and location_y, or neither")
        return self


class EventAIUpdateRequest(BaseModel):
    player_id: Optional[int] = None
    clip_path: Optional[str] = None
    injury_note: Optional[str] = None
    identified_by: Optional[IdentifiedByText] = None
    id_detail: Optional[str] = None
    id_confidence: Optional[Confidence] = None
    player_track_id: Optional[DatabaseId] = None


class AssignPlayerRequest(BaseModel):
    player_id: int


class ManualEventRequest(BaseModel):
    player_id: int
    event_type: EventTypeText
    note: str
    risk: Optional[RiskLevel] = "MEDIUM"
    match_session_id: Optional[int] = None


class EventCreateResponse(BaseModel):
    message: str
    event_id: int
    duplicate: bool = False          # True when an existing event was matched instead of creating a new one


class InjuryEventRead(ORMModel):
    """Same keys as main.py event_to_dict(), plus the new fields."""

    id: int
    player_id: Optional[int] = None
    player_name: str = "Unidentified"
    jersey_number: Optional[int] = None
    team_name: Optional[str] = None
    identified: bool = False
    identified_at: Optional[datetime] = None
    identified_by: Optional[str] = None
    id_detail: Optional[str] = None
    id_confidence: Optional[float] = None
    track_id: Optional[int] = None
    player_track_id: Optional[int] = None
    video_id: Optional[int] = None
    collision_event_id: Optional[int] = None
    video_time_sec: Optional[float] = None
    frame_number: Optional[int] = None
    match_session_id: Optional[int] = None
    match_name: Optional[str] = None
    event_type: str
    region: Optional[str] = None
    side: Optional[str] = None
    risk: Optional[str] = None
    risk_score: Optional[float] = None
    confidence: Optional[float] = None
    movement: Optional[float] = None
    note: Optional[str] = None
    injury_note: Optional[str] = None
    clip_url: Optional[str] = None
    source: str = "ai"
    resolved: Optional[bool] = False
    resolved_at: Optional[datetime] = None
    location_x: Optional[float] = None
    location_y: Optional[float] = None
    detection_count: int = 1
    timestamp: Optional[datetime] = None

    @classmethod
    def from_event(cls, e: Any) -> "InjuryEventRead":
        """Builds the response from an InjuryEvent ORM object."""

        player = e.player
        match = e.match

        if match is not None and match.name:
            match_name = match.name
        elif e.match_session_id:
            match_name = "Match #" + str(e.match_session_id)
        else:
            match_name = None

        data: Dict[str, Any] = {
            "id": e.id,
            "player_id": e.player_id,
            "player_name": player.name if player is not None else "Unidentified",
            "jersey_number": player.jersey_number if player is not None else None,
            "team_name": player.team.name if player is not None and player.team is not None else None,
            "identified": e.player_id is not None,
            "identified_at": e.identified_at,
            "identified_by": e.identified_by,
            "id_detail": e.id_detail,
            "id_confidence": e.id_confidence,
            "track_id": e.track_id,
            "player_track_id": e.player_track_id,
            "video_id": e.video_id,
            "collision_event_id": e.collision_event_id,
            "video_time_sec": e.video_time_sec,
            "frame_number": e.frame_number,
            "match_session_id": e.match_session_id,
            "match_name": match_name,
            "event_type": e.event_type,
            "region": e.region,
            "side": e.side,
            "risk": e.risk,
            "risk_score": e.risk_score,
            "confidence": e.confidence,
            "movement": e.movement,
            "note": e.note,
            "injury_note": e.injury_note,
            "clip_url": (CLIPS_URL_PREFIX + e.clip_path) if e.clip_path else None,
            "source": e.source,
            "resolved": e.resolved,
            "resolved_at": e.resolved_at,
            "location_x": e.location_x,
            "location_y": e.location_y,
            "detection_count": e.detection_count or 1,
            "timestamp": e.timestamp,
        }

        return cls(**data)


class MedicalAssessmentRead(BaseModel):
    """Generated, non-persistent AI screening summary derived from a stored incident."""

    id: str
    injury_event_id: Optional[int] = None
    collision_event_id: Optional[int] = None
    player_id: Optional[int] = None
    player_name: str = "Player"
    event_type: str
    affected_region: str
    risk_level: Optional[RiskLevel] = None
    observed_signs: List[str] = Field(default_factory=list)
    assessment_summary: str
    recommended_action: str
    requires_medical_attention: bool
    confidence: Optional[Confidence] = None
    evidence_available: bool
    evidence_clip_url: Optional[str] = None
    frame_number: Optional[int] = None
    video_time_sec: Optional[float] = None
    collision_context: Optional[str] = None
    disclaimer: str
    created_at: datetime


# ============================================
# COLLISION EVENTS  (two participants, each with track_id AND player_id)
# ============================================

class CollisionParticipantCreate(BaseModel):
    track_id: TrackId                                   # tracker number
    player_id: Optional[DatabaseId] = None              # players.id, None while unidentified
    player_track_id: Optional[DatabaseId] = None
    slot: Optional[int] = None                          # 1 or 2; assigned by order when omitted
    identified_by: Optional[IdentifiedByText] = None
    id_detail: Optional[str] = None
    id_confidence: Optional[Confidence] = None
    region: Optional[str] = Field(default=None, max_length=50)
    movement: Optional[float] = None

    @model_validator(mode="after")
    def check_slot(self):
        if self.slot is not None and self.slot not in COLLISION_PARTICIPANT_SLOTS:
            raise ValueError("slot must be one of: " + ", ".join(str(s) for s in COLLISION_PARTICIPANT_SLOTS))
        return self


class CollisionEventCreate(BaseModel):
    match_session_id: Optional[DatabaseId] = None
    video_id: Optional[DatabaseId] = None
    risk: Optional[RiskLevel] = None
    risk_score: Optional[float] = None
    confidence: Optional[Confidence] = None             # event confidence
    iou: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    contact_frames: Optional[int] = Field(default=None, ge=0)
    frame_number: Optional[int] = Field(default=None, ge=0)
    video_time_sec: Optional[float] = Field(default=None, ge=0)
    location_x: Optional[NormalizedCoord] = None        # collision location, normalised
    location_y: Optional[NormalizedCoord] = None
    location_bbox: Optional[List[NormalizedCoord]] = Field(default=None, min_length=4, max_length=4)
    region: Optional[str] = Field(default=None, max_length=50)
    note: Optional[str] = None
    clip_path: Optional[str] = None
    source: EventSource = "ai"
    dedupe_key: Optional[DedupeKey] = None
    participants: List[CollisionParticipantCreate] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def check_collision(self):

        a, b = self.participants

        if a.track_id == b.track_id:
            raise ValueError("a collision needs two different track ids")

        if a.player_id is not None and a.player_id == b.player_id:
            raise ValueError("a collision needs two different players")

        if a.player_track_id is not None and a.player_track_id == b.player_track_id:
            raise ValueError("a collision needs two different player_track_id values")

        if a.slot is not None or b.slot is not None:
            if a.slot is None or b.slot is None:
                raise ValueError("give slot 1 and slot 2 to both participants, or to neither")
            if sorted((a.slot, b.slot)) != sorted(COLLISION_PARTICIPANT_SLOTS):
                raise ValueError("the two participants must use slots 1 and 2, one each")

        if (self.location_x is None) != (self.location_y is None):
            raise ValueError("give both location_x and location_y, or neither")

        if self.location_bbox is not None:
            x1, y1, x2, y2 = self.location_bbox
            if x2 <= x1 or y2 <= y1:
                raise ValueError("location_bbox must be [x1, y1, x2, y2] with x2 > x1 and y2 > y1")

        return self

    def slotted_participants(self) -> List[Tuple[int, CollisionParticipantCreate]]:
        """Returns [(slot, participant), ...] ordered by slot; slots follow list order when omitted."""

        first, second = self.participants

        if first.slot is None:
            pairs = [(COLLISION_PARTICIPANT_SLOTS[0], first), (COLLISION_PARTICIPANT_SLOTS[1], second)]
        else:
            pairs = [(first.slot, first), (second.slot, second)]

        return sorted(pairs, key=lambda pair: pair[0])


class CollisionEventUpdate(BaseModel):
    risk: Optional[RiskLevel] = None
    risk_score: Optional[float] = None
    confidence: Optional[Confidence] = None
    clip_path: Optional[str] = None
    note: Optional[str] = None
    resolved: Optional[bool] = None


class CollisionParticipantAssign(BaseModel):
    """Fills in / corrects the player of ONE participant (chosen by slot in the URL)."""
    player_id: DatabaseId
    player_track_id: Optional[DatabaseId] = None
    identified_by: Optional[IdentifiedByText] = None
    id_detail: Optional[str] = None
    id_confidence: Optional[Confidence] = None


class CollisionParticipantRead(ORMModel):
    id: int
    collision_event_id: Optional[int] = None
    slot: int
    track_id: int
    player_id: Optional[int] = None
    player_track_id: Optional[int] = None
    identified_by: Optional[str] = None
    id_detail: Optional[str] = None
    id_confidence: Optional[float] = None
    identified_at: Optional[datetime] = None
    region: Optional[str] = None
    movement: Optional[float] = None
    player: Optional[PlayerBrief] = None


class CollisionEventRead(ORMModel):
    id: int
    match_session_id: Optional[int] = None
    video_id: Optional[int] = None
    event_type: str = EVENT_TYPE_COLLISION
    risk: Optional[str] = None
    risk_score: Optional[float] = None
    confidence: Optional[float] = None
    iou: Optional[float] = None
    contact_frames: Optional[int] = None
    frame_number: Optional[int] = None
    video_time_sec: Optional[float] = None
    location_x: Optional[float] = None
    location_y: Optional[float] = None
    location_bbox: Optional[List[float]] = None
    region: Optional[str] = None
    note: Optional[str] = None
    clip_path: Optional[str] = Field(default=None, exclude=True)
    source: str = "ai"
    resolved: bool = False
    resolved_at: Optional[datetime] = None
    detection_count: int = 1
    timestamp: Optional[datetime] = None
    participants: List[CollisionParticipantRead] = Field(default_factory=list)
    track_ids: List[int] = Field(default_factory=list)       # tracker numbers of the two participants
    player_ids: List[int] = Field(default_factory=list)      # database players already identified

    @computed_field
    @property
    def clip_url(self) -> Optional[str]:
        return (CLIPS_URL_PREFIX + self.clip_path) if self.clip_path else None


class CollisionCreateResponse(BaseModel):
    message: str
    collision_event_id: int
    duplicate: bool = False


# ============================================
# ALERTS
# ============================================

class AlertCreate(BaseModel):
    injury_event_id: Optional[DatabaseId] = None
    collision_event_id: Optional[DatabaseId] = None
    recipient_user_id: Optional[DatabaseId] = None
    team_id: Optional[DatabaseId] = None
    alert_type: AlertType = "NEW_EVENT"
    severity: Optional[RiskLevel] = None
    channel: AlertChannel = "web"
    title: Optional[str] = Field(default=None, max_length=200)
    message: str = Field(min_length=1)
    dedupe_key: Optional[DedupeKey] = None

    @model_validator(mode="after")
    def needs_an_event(self):
        if self.injury_event_id is None and self.collision_event_id is None:
            raise ValueError("an alert must reference an injury_event_id or a collision_event_id")
        return self


class AlertStatusUpdate(BaseModel):
    status: AlertStatus
    error_message: Optional[str] = None


class AlertMarkRead(BaseModel):
    """alert_ids omitted = mark every visible alert as read."""
    alert_ids: Optional[List[DatabaseId]] = None


class AlertRead(ORMModel):
    id: int
    injury_event_id: Optional[int] = None
    collision_event_id: Optional[int] = None
    recipient_user_id: Optional[int] = None
    team_id: Optional[int] = None
    alert_type: str
    severity: Optional[str] = None
    channel: str
    title: Optional[str] = None
    message: str
    status: str
    sent_at: Optional[datetime] = None
    error_message: Optional[str] = None
    is_read: bool = False
    read_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


__all__ = [
    "AI_EVENT_TYPES",
    "CLIPS_URL_PREFIX",
    "AlertChannel",
    "AlertCreate",
    "AlertMarkRead",
    "AlertRead",
    "AlertStatus",
    "AlertStatusUpdate",
    "AlertType",
    "AssignPlayerRequest",
    "CollisionCreateResponse",
    "CollisionEventCreate",
    "CollisionEventRead",
    "CollisionEventUpdate",
    "CollisionParticipantAssign",
    "CollisionParticipantCreate",
    "CollisionParticipantRead",
    "Confidence",
    "DatabaseId",
    "DedupeKey",
    "EmailAddress",
    "EventAIUpdateRequest",
    "EventCreateRequest",
    "EventCreateResponse",
    "EventSource",
    "EventTypeText",
    "IdentifiedByText",
    "IdentityStatus",
    "InjuryEventRead",
    "LoginRequest",
    "ManualEventRequest",
    "MedicalAssessmentRead",
    "MatchCreateRequest",
    "MatchRead",
    "MatchStatus",
    "MatchUpdate",
    "MessageResponse",
    "NormalizedCoord",
    "ORMModel",
    "PasswordChangeRequest",
    "PlayerBrief",
    "PlayerCreate",
    "PlayerRead",
    "PlayerStatsRead",
    "PlayerTrackRead",
    "PlayerTrackUpsert",
    "PlayerUpdate",
    "RiskLevel",
    "RoleName",
    "TeamCreate",
    "TeamRead",
    "TeamSummaryRead",
    "TeamUpdate",
    "TokenResponse",
    "TrackId",
    "UserCreate",
    "UserRead",
    "UserUpdate",
    "VideoCreate",
    "VideoRead",
    "VideoSourceType",
    "VideoStatus",
    "VideoUpdate",
]