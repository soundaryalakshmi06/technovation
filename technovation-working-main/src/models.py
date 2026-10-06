"""
AthleteGuard - SQLAlchemy models (PostgreSQL).

Compatible with the existing backend: Team, User, Player, MatchSession and
InjuryEvent keep every existing table name, column name and column type
(main.py imports exactly these). New columns are nullable or have defaults.

Identity model (the key design rule)
------------------------------------
    track_id   = the number the tracker (ByteTrack / BoT-SORT) gave a person
                 in ONE video run. It is not stable and it is NOT a player.
    player_id  = players.id in the database - a real person.

They are never mixed:
    * player_tracks maps (match_session_id, track_id) -> player_id
      (player_id stays NULL until the player is identified).
    * injury_events and collision_participants store BOTH track_id (what the
      tracker saw) and player_id (who it really was), plus an optional
      player_track_id pointing at the player_tracks row.

Collisions and falls
--------------------
    collision_events        one row per collision (location, confidence, ...)
    collision_participants  exactly two rows per collision (slot 1 and 2), each
                            with its own track_id and player_id
    injury_events           one row per fall / injury for the EXACT affected
                            player; collision_event_id links a fall that
                            followed a collision

De-duplication
--------------
    dedupe_key      exact, idempotent key (unique per match) so a retried POST
                    or the same detection reported twice cannot create a copy
    detection_count how many raw detections were merged into the row
    The time/space window logic (cooldown, incident merge) lives in the service
    layer and uses the indexes declared here.
"""

from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import expression, func

from config import (
    COLLISION_PARTICIPANT_SLOTS,
    EVENT_TYPE_COLLISION,
    EVENT_TYPE_FALL,
    IDENTITY_UNKNOWN,
    settings,
)
from database import Base


# ============================================
# HELPERS
# ============================================

class TimestampMixin:
    """created_at / updated_at for tables that have no event 'timestamp'."""

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def _range_01(column: str) -> str:
    return "%s IS NULL OR (%s >= 0 AND %s <= 1)" % (column, column, column)


def _time_bucket(video_time_sec: Optional[float]) -> Optional[int]:
    if video_time_sec is None:
        return None
    return int(float(video_time_sec) // settings.DEDUPE_BUCKET_SECONDS)


def build_fall_dedupe_key(
    match_session_id: Optional[int],
    track_id: Optional[int],
    video_time_sec: Optional[float],
    player_id: Optional[int] = None,
    event_type: str = EVENT_TYPE_FALL,
) -> Optional[str]:
    """
    Idempotency key for a fall / injury. Uses the database player when it is
    already known, otherwise the tracker id. Returns None when the video time
    is unknown (without a time a key would wrongly merge different falls).
    """
    bucket = _time_bucket(video_time_sec)

    if bucket is None or (player_id is None and track_id is None):
        return None

    subject = ("p%d" % player_id) if player_id is not None else ("t%d" % track_id)

    return "%s:m%s:%s:b%d" % (str(event_type).upper(), match_session_id or 0, subject, bucket)


def build_collision_dedupe_key(
    match_session_id: Optional[int],
    track_id_a: int,
    track_id_b: int,
    video_time_sec: Optional[float],
) -> Optional[str]:
    """
    Idempotency key for a collision. Order independent: (3, 9) and (9, 3)
    give the same key. Returns None when the video time is unknown.
    """
    bucket = _time_bucket(video_time_sec)

    if bucket is None:
        return None

    low, high = sorted((int(track_id_a), int(track_id_b)))

    return "%s:m%s:t%d-t%d:b%d" % (EVENT_TYPE_COLLISION, match_session_id or 0, low, high, bucket)


# ============================================
# TEAM
# ============================================

class Team(TimestampMixin, Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    short_name = Column(String(20), nullable=True)
    active = Column(Boolean, nullable=False, default=True, server_default=expression.true())

    players = relationship("Player", back_populates="team")
    users = relationship("User", back_populates="team")


# ============================================
# USER
# role: admin / coach / medical / player (see config.ROLES)
# Deactivating a user (active = False) blocks login but keeps history.
# ============================================

class User(TimestampMixin, Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    player_id = Column(Integer, ForeignKey("players.id", ondelete="SET NULL"), nullable=True, unique=True, index=True)

    full_name = Column(String(120), nullable=True)
    email = Column(String(255), unique=True, index=True, nullable=True)
    phone = Column(String(32), nullable=True)           # used for WhatsApp alerts
    active = Column(Boolean, nullable=False, default=True, server_default=expression.true())
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    team = relationship("Team", back_populates="users")
    player = relationship("Player", back_populates="user", uselist=False)


# ============================================
# PLAYER
# A real person. Never holds a tracker id.
# ============================================

class Player(TimestampMixin, Base):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    name = Column(String, nullable=False)
    jersey_number = Column(Integer, nullable=False)

    position = Column(String(50), nullable=True)
    active = Column(Boolean, nullable=False, default=True, server_default=expression.true())
    reference_images = Column(JSON, nullable=True)      # list of image paths
    face_embedding = Column(JSON, nullable=True)        # list of floats (optional)

    team = relationship("Team", back_populates="players")
    user = relationship("User", back_populates="player", uselist=False)
    tracks = relationship("PlayerTrack", back_populates="player")
    injury_events = relationship("InjuryEvent", back_populates="player")
    collision_participations = relationship("CollisionParticipant", back_populates="player")

    @property
    def team_name(self) -> Optional[str]:
        return self.team.name if self.team is not None else None

    __table_args__ = (
        Index("ix_players_team_jersey", "team_id", "jersey_number"),
    )


# ============================================
# MATCH SESSION
# One row per match / detection run. Table name unchanged.
# ============================================

class MatchSession(TimestampMixin, Base):
    __tablename__ = "match_sessions"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=True)
    video_source = Column(String, nullable=True)
    date = Column(DateTime(timezone=True), server_default=func.now())
    team_a_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    team_b_id = Column(Integer, ForeignKey("teams.id"), nullable=True)

    status = Column(String(20), nullable=False, default="CREATED", server_default="CREATED")
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    team_a = relationship("Team", foreign_keys=[team_a_id])
    team_b = relationship("Team", foreign_keys=[team_b_id])
    created_by = relationship("User", foreign_keys=[created_by_id])

    videos = relationship("Video", back_populates="match", cascade="all, delete-orphan", passive_deletes=True)
    player_tracks = relationship("PlayerTrack", back_populates="match", cascade="all, delete-orphan", passive_deletes=True)
    collision_events = relationship("CollisionEvent", back_populates="match", cascade="all, delete-orphan", passive_deletes=True)
    injury_events = relationship("InjuryEvent", back_populates="match")


# ============================================
# VIDEO
# A video (or live source) analysed for a match.
# ============================================

class Video(TimestampMixin, Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True)
    match_session_id = Column(Integer, ForeignKey("match_sessions.id", ondelete="CASCADE"), nullable=False)

    source_type = Column(String(20), nullable=False, default="file", server_default="file")
    original_filename = Column(String(255), nullable=True)
    file_path = Column(String(500), nullable=True)
    content_type = Column(String(100), nullable=True)
    file_size_bytes = Column(BigInteger, nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)

    fps = Column(Float, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    total_frames = Column(Integer, nullable=True)
    duration_sec = Column(Float, nullable=True)

    status = Column(String(20), nullable=False, default="UPLOADED", server_default="UPLOADED")
    processed_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    match = relationship("MatchSession", back_populates="videos")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])

    __table_args__ = (
        Index("ix_videos_match", "match_session_id"),
        # the same file cannot be attached twice to the same match
        Index("uq_videos_match_checksum", "match_session_id", "checksum_sha256", unique=True),
        CheckConstraint("fps IS NULL OR fps > 0", name="fps_positive"),
        CheckConstraint("duration_sec IS NULL OR duration_sec >= 0", name="duration_non_negative"),
    )


# ============================================
# PLAYER TRACK  (player identity <-> tracking relationship)
# One row per tracker id per match. player_id is NULL until identified.
# predecessor_id records identity hand-over when the tracker renumbers the
# same person (old track -> new track).
# ============================================

class PlayerTrack(TimestampMixin, Base):
    __tablename__ = "player_tracks"

    id = Column(Integer, primary_key=True)
    match_session_id = Column(Integer, ForeignKey("match_sessions.id", ondelete="CASCADE"), nullable=False)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True)

    track_id = Column(Integer, nullable=False)                      # tracker id, NOT a player id
    player_id = Column(Integer, ForeignKey("players.id", ondelete="SET NULL"), nullable=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)

    identity_status = Column(String(20), nullable=False, default=IDENTITY_UNKNOWN, server_default=IDENTITY_UNKNOWN)
    identified_by = Column(String, nullable=True)                   # jersey / face / manual ...
    id_detail = Column(String, nullable=True)
    id_confidence = Column(Float, nullable=True)                    # identity confidence (not injury confidence)
    identified_at = Column(DateTime(timezone=True), nullable=True)

    predecessor_id = Column(Integer, ForeignKey("player_tracks.id", ondelete="SET NULL"), nullable=True)

    first_frame = Column(Integer, nullable=True)
    last_frame = Column(Integer, nullable=True)
    first_seen_sec = Column(Float, nullable=True)
    last_seen_sec = Column(Float, nullable=True)
    tracking_confidence = Column(Float, nullable=True)

    match = relationship("MatchSession", back_populates="player_tracks")
    video = relationship("Video", foreign_keys=[video_id])
    player = relationship("Player", back_populates="tracks")
    team = relationship("Team", foreign_keys=[team_id])
    predecessor = relationship("PlayerTrack", remote_side="PlayerTrack.id", foreign_keys=[predecessor_id])

    __table_args__ = (
        # one tracker id = one row per match
        Index("uq_player_tracks_match_track", "match_session_id", "track_id", unique=True),
        Index("ix_player_tracks_player", "player_id"),
        Index("ix_player_tracks_match_player", "match_session_id", "player_id"),
        CheckConstraint(_range_01("id_confidence"), name="id_confidence_range"),
        CheckConstraint(_range_01("tracking_confidence"), name="tracking_confidence_range"),
        CheckConstraint(
            "identity_status IN ('CONFIRMED', 'UNCERTAIN', 'UNKNOWN')",
            name="identity_status_valid",
        ),
    )


# ============================================
# COLLISION EVENT
# One row per collision between two tracked people.
# The two people are in collision_participants.
# ============================================

class CollisionEvent(Base):
    __tablename__ = "collision_events"

    id = Column(Integer, primary_key=True)
    match_session_id = Column(Integer, ForeignKey("match_sessions.id", ondelete="CASCADE"), nullable=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True)

    event_type = Column(String(30), nullable=False, default=EVENT_TYPE_COLLISION, server_default=EVENT_TYPE_COLLISION)
    risk = Column(String, nullable=True)
    risk_score = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)           # event (detection) confidence, 0..1
    iou = Column(Float, nullable=True)                  # overlap of the two boxes at confirmation
    contact_frames = Column(Integer, nullable=True)     # consecutive frames the contact lasted

    frame_number = Column(Integer, nullable=True)
    video_time_sec = Column(Float, nullable=True)

    # Collision location, normalised to the frame (0..1) so it is resolution independent
    location_x = Column(Float, nullable=True)
    location_y = Column(Float, nullable=True)
    location_bbox = Column(JSON, nullable=True)         # [x1, y1, x2, y2], normalised

    region = Column(String(50), nullable=True)          # body area of contact, when known
    note = Column(String, nullable=True)
    clip_path = Column(String, nullable=True)
    source = Column(String, nullable=False, default="ai", server_default="ai")
    resolved = Column(Boolean, nullable=False, default=False, server_default=expression.false())
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    dedupe_key = Column(String(255), nullable=True)
    detection_count = Column(Integer, nullable=False, default=1, server_default="1")

    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    match = relationship("MatchSession", back_populates="collision_events")
    video = relationship("Video", foreign_keys=[video_id])

    participants = relationship(
        "CollisionParticipant",
        back_populates="collision",
        order_by="CollisionParticipant.slot",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    injury_events = relationship("InjuryEvent", back_populates="collision_event")
    alerts = relationship("Alert", back_populates="collision_event", cascade="all, delete-orphan", passive_deletes=True)

    __table_args__ = (
        Index("ix_collision_events_match_time", "match_session_id", "video_time_sec"),
        Index("ix_collision_events_timestamp", "timestamp"),
        Index("uq_collision_match_dedupe", "match_session_id", "dedupe_key", unique=True),
        CheckConstraint(_range_01("confidence"), name="confidence_range"),
        CheckConstraint(_range_01("location_x"), name="location_x_range"),
        CheckConstraint(_range_01("location_y"), name="location_y_range"),
        CheckConstraint("detection_count >= 1", name="detection_count_positive"),
    )

    @property
    def track_ids(self):
        return [p.track_id for p in self.participants]

    @property
    def player_ids(self):
        return [p.player_id for p in self.participants if p.player_id is not None]


# ============================================
# COLLISION PARTICIPANT
# Exactly two rows per collision (slot 1 and slot 2). Each row keeps the
# tracker id AND the database player separately.
# player_id is NULL while that person is still unidentified.
# ============================================

class CollisionParticipant(TimestampMixin, Base):
    __tablename__ = "collision_participants"

    id = Column(Integer, primary_key=True)
    collision_event_id = Column(Integer, ForeignKey("collision_events.id", ondelete="CASCADE"), nullable=False)
    slot = Column(SmallInteger, nullable=False)                     # 1 or 2

    track_id = Column(Integer, nullable=False)                      # tracker id, NOT a player id
    player_id = Column(Integer, ForeignKey("players.id", ondelete="SET NULL"), nullable=True)
    player_track_id = Column(Integer, ForeignKey("player_tracks.id", ondelete="SET NULL"), nullable=True)

    identified_by = Column(String, nullable=True)
    id_detail = Column(String, nullable=True)
    id_confidence = Column(Float, nullable=True)
    identified_at = Column(DateTime(timezone=True), nullable=True)

    region = Column(String(50), nullable=True)                      # this person's contact area
    movement = Column(Float, nullable=True)                         # movement score at contact

    collision = relationship("CollisionEvent", back_populates="participants")
    player = relationship("Player", back_populates="collision_participations")
    player_track = relationship("PlayerTrack", foreign_keys=[player_track_id])

    __table_args__ = (
        Index("uq_collision_participants_slot", "collision_event_id", "slot", unique=True),
        Index("uq_collision_participants_track", "collision_event_id", "track_id", unique=True),
        # the same real player cannot be both participants (NULLs are allowed twice)
        Index("uq_collision_participants_player", "collision_event_id", "player_id", unique=True),
        Index("ix_collision_participants_player", "player_id"),
        CheckConstraint(
            "slot IN (%s)" % ", ".join(str(s) for s in COLLISION_PARTICIPANT_SLOTS),
            name="slot_valid",
        ),
        CheckConstraint(_range_01("id_confidence"), name="id_confidence_range"),
    )


# ============================================
# INJURY EVENT  (fall / injury record for ONE exact player)
# Existing table and columns are unchanged - main.py keeps working.
# player_id is nullable: a fall is saved as "Unidentified" first and the
# player is filled in later on the same row.
# ============================================

class InjuryEvent(Base):
    __tablename__ = "injury_events"

    # ---- existing columns (unchanged) ----
    id = Column(Integer, primary_key=True, index=True)
    match_session_id = Column(Integer, ForeignKey("match_sessions.id"), nullable=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=True)
    event_type = Column(String, nullable=False)
    region = Column(String, nullable=True)
    risk = Column(String, nullable=True)
    movement = Column(Float, nullable=True)
    note = Column(String, nullable=True)
    injury_note = Column(String, nullable=True)
    clip_path = Column(String, nullable=True)
    source = Column(String, nullable=False, default="ai", server_default="ai")
    resolved = Column(Boolean, default=False, server_default=expression.false())
    track_id = Column(Integer, nullable=True)
    video_time_sec = Column(Float, nullable=True)
    identified_by = Column(String, nullable=True)
    id_detail = Column(String, nullable=True)
    id_confidence = Column(Float, nullable=True)
    identified_at = Column(DateTime(timezone=True), nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    # ---- new columns (all nullable / defaulted) ----
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True)
    player_track_id = Column(Integer, ForeignKey("player_tracks.id", ondelete="SET NULL"), nullable=True)
    collision_event_id = Column(Integer, ForeignKey("collision_events.id", ondelete="SET NULL"), nullable=True)

    side = Column(String(10), nullable=True)                        # left / right
    confidence = Column(Float, nullable=True)                       # event (injury) confidence, 0..1 - separate from id_confidence
    risk_score = Column(Float, nullable=True)
    frame_number = Column(Integer, nullable=True)
    location_x = Column(Float, nullable=True)                       # normalised 0..1
    location_y = Column(Float, nullable=True)

    dedupe_key = Column(String(255), nullable=True)
    detection_count = Column(Integer, nullable=False, default=1, server_default="1")

    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    player = relationship("Player", back_populates="injury_events")
    match = relationship("MatchSession", back_populates="injury_events")
    video = relationship("Video", foreign_keys=[video_id])
    player_track = relationship("PlayerTrack", foreign_keys=[player_track_id])
    collision_event = relationship("CollisionEvent", back_populates="injury_events")
    resolved_by = relationship("User", foreign_keys=[resolved_by_id])
    alerts = relationship("Alert", back_populates="injury_event", cascade="all, delete-orphan", passive_deletes=True)

    __table_args__ = (
        Index("ix_injury_events_player", "player_id"),
        Index("ix_injury_events_match_track", "match_session_id", "track_id"),
        Index("ix_injury_events_match_time", "match_session_id", "video_time_sec"),
        Index("ix_injury_events_collision", "collision_event_id"),
        Index("ix_injury_events_timestamp", "timestamp"),
        Index("uq_injury_match_dedupe", "match_session_id", "dedupe_key", unique=True),
    )


# ============================================
# ALERT
# One row per notification (a web notification or a WhatsApp message) about a
# fall/injury or a collision. dedupe_key is unique, so the same alert for the
# same event/recipient/channel cannot be created twice.
# ============================================

class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True)

    injury_event_id = Column(Integer, ForeignKey("injury_events.id", ondelete="CASCADE"), nullable=True)
    collision_event_id = Column(Integer, ForeignKey("collision_events.id", ondelete="CASCADE"), nullable=True)

    recipient_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)

    alert_type = Column(String(30), nullable=False, default="NEW_EVENT", server_default="NEW_EVENT")
    severity = Column(String(10), nullable=True)                    # LOW / MEDIUM / HIGH / CRITICAL
    channel = Column(String(20), nullable=False, default="web", server_default="web")
    title = Column(String(200), nullable=True)
    message = Column(Text, nullable=False)

    status = Column(String(20), nullable=False, default="PENDING", server_default="PENDING")
    sent_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)

    is_read = Column(Boolean, nullable=False, default=False, server_default=expression.false())
    read_at = Column(DateTime(timezone=True), nullable=True)

    dedupe_key = Column(String(255), unique=True, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    injury_event = relationship("InjuryEvent", back_populates="alerts")
    collision_event = relationship("CollisionEvent", back_populates="alerts")
    recipient = relationship("User", foreign_keys=[recipient_user_id])
    team = relationship("Team", foreign_keys=[team_id])

    __table_args__ = (
        Index("ix_alerts_injury_event", "injury_event_id"),
        Index("ix_alerts_collision_event", "collision_event_id"),
        Index("ix_alerts_recipient_unread", "recipient_user_id", "is_read"),
        Index("ix_alerts_team", "team_id"),
        Index("ix_alerts_created_at", "created_at"),
        CheckConstraint(
            "injury_event_id IS NOT NULL OR collision_event_id IS NOT NULL",
            name="has_event",
        ),
    )


__all__ = [
    "Alert",
    "CollisionEvent",
    "CollisionParticipant",
    "InjuryEvent",
    "MatchSession",
    "Player",
    "PlayerTrack",
    "Team",
    "TimestampMixin",
    "User",
    "Video",
    "build_collision_dedupe_key",
    "build_fall_dedupe_key",
]
