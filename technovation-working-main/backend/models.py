from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Float
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)

    players = relationship("Player", back_populates="team")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=True)

    team = relationship("Team")


class Player(Base):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    name = Column(String, nullable=False)
    jersey_number = Column(Integer, nullable=False)

    team = relationship("Team", back_populates="players")


# ============================================
# MATCH SESSION
# One row per detection run / video, so events
# from different matches can be told apart.
# ============================================

class MatchSession(Base):
    __tablename__ = "match_sessions"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=True)
    video_source = Column(String, nullable=True)
    date = Column(DateTime(timezone=True), server_default=func.now())
    team_a_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    team_b_id = Column(Integer, ForeignKey("teams.id"), nullable=True)


# ============================================
# INJURY EVENT
# player_id is nullable: a fall is saved right
# away as "Unidentified" and the player is filled
# in later on the same row once identified.
# identified_by / id_detail / id_confidence record
# HOW the player was recognised (jersey or face).
# ============================================

class InjuryEvent(Base):
    __tablename__ = "injury_events"

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
    source = Column(String, nullable=False, default="ai")
    resolved = Column(Boolean, default=False)
    track_id = Column(Integer, nullable=True)
    video_time_sec = Column(Float, nullable=True)
    identified_by = Column(String, nullable=True)
    id_detail = Column(String, nullable=True)
    id_confidence = Column(Float, nullable=True)
    identified_at = Column(DateTime(timezone=True), nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    player = relationship("Player")
    match = relationship("MatchSession")