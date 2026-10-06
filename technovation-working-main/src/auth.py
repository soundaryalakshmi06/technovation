"""
AthleteGuard / Technovation - authentication (JWT + bcrypt).

What lives here
  * password hashing        hash_password / verify_password        (passlib + bcrypt, same scheme as before,
                                                                   so every existing password hash keeps working)
  * JWT                     create_access_token / decode_access_token / decode_token_strict
  * login                   authenticate_user / login_user  ->  schemas.TokenResponse
  * current user lookup     get_user_from_token  (used by dependencies.py and, later, by WebSockets)
  * user management         create_user / update_user / ensure_bootstrap_admin

Design rules
  * Every secret comes from config.settings. Nothing is hard-coded here.
  * Authorisation NEVER trusts the role / team_id stored inside a token. Tokens only identify the user;
    role, team and the active flag are always re-read from PostgreSQL, so deactivating an account or
    changing a role takes effect immediately.
  * Errors are AuthError (an HTTPException), so FastAPI turns them into clean 401 / 403 / 409 responses
    without any extra handler. Messages never reveal whether a username exists.
  * Import root is src/ ("from config import settings") - the same as the AI scripts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import ROLE_ADMIN, ROLE_COACH, ROLE_PLAYER, ROLES, settings
from models import Player, Team, User
from schemas import LoginRequest, TokenResponse, UserCreate, UserUpdate

logger = logging.getLogger("athleteguard.auth")

# Same scheme as the original project: existing bcrypt hashes in the users table verify unchanged.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Verified against when the username does not exist, so "unknown user" and "wrong password"
# take the same time and cannot be told apart by timing.
_DUMMY_HASH = pwd_context.hash("athleteguard-timing-equaliser")

# Kept as module attributes because the original auth.py exposed them.
ALGORITHM = settings.JWT_ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES


# ===========================================================================
# Errors (HTTPException subclasses -> FastAPI renders them directly)
# ===========================================================================
class AuthError(HTTPException):
    status_code_default = 401
    detail_default = "Authentication failed"

    def __init__(self, detail: Optional[str] = None, status_code: Optional[int] = None):
        code = status_code or self.status_code_default
        headers = {"WWW-Authenticate": "Bearer"} if code == 401 else None
        super().__init__(status_code=code, detail=detail or self.detail_default, headers=headers)


class InvalidCredentialsError(AuthError):
    detail_default = "Invalid username or password"


class MissingTokenError(AuthError):
    detail_default = "Not authenticated"


class InvalidTokenError(AuthError):
    detail_default = "Invalid or expired token"


class TokenExpiredError(AuthError):
    detail_default = "Token has expired"


class UserNotFoundError(AuthError):
    detail_default = "User not found"


class InactiveUserError(AuthError):
    status_code_default = 403
    detail_default = "Account is disabled"


class ForbiddenError(AuthError):
    status_code_default = 403
    detail_default = "You do not have permission to perform this action"


class UserConflictError(AuthError):
    status_code_default = 409
    detail_default = "Username already exists"


# ===========================================================================
# Passwords
# ===========================================================================
def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(password.encode("utf-8")) > 72:
        raise ValueError("password must be at most 72 bytes (bcrypt limit)")
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """True/False only - a malformed hash or odd input is a failed login, never an exception."""
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except (ValueError, TypeError):
        return False


# ===========================================================================
# JWT
# ===========================================================================
def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """Claims kept from the original (user_id, role, team_id) plus sub / iat / exp."""
    now = datetime.now(timezone.utc)
    expires = now + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    payload = dict(data)
    payload.update({"iat": int(now.timestamp()), "exp": int(expires.timestamp())})
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token_strict(token: str) -> Dict[str, Any]:
    """Decode and verify signature + expiry. Raises TokenExpiredError / InvalidTokenError."""
    if not token or not token.strip():
        raise MissingTokenError()
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except ExpiredSignatureError:
        raise TokenExpiredError()
    except JWTError:
        raise InvalidTokenError()
    if not isinstance(payload.get("user_id"), int):
        raise InvalidTokenError()
    return payload


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Original behaviour: the payload, or None when the token is invalid or expired."""
    try:
        return decode_token_strict(token)
    except AuthError:
        return None


# ===========================================================================
# Login / current user
# ===========================================================================
def authenticate_user(db: Session, username: str, password: str) -> User:
    """
    Return the user when username + password are right and the account is active.
    Wrong username and wrong password produce the same error; a disabled account is only revealed
    after the correct password was supplied.
    """
    identifier = username.strip()
    matches = db.query(User).filter(
        or_(
            func.lower(User.username) == identifier.lower(),
            func.lower(User.email) == identifier.lower(),
        )
    ).limit(2).all()
    if len(matches) != 1:
        verify_password(password, _DUMMY_HASH)
        raise InvalidCredentialsError()
    user = matches[0]
    if not verify_password(password, user.password_hash):
        raise InvalidCredentialsError()
    if not user.active:
        raise InactiveUserError()
    if user.role == ROLE_PLAYER and (user.player is None or not user.player.active):
        raise InactiveUserError()
    return user


def build_token_response(user: User) -> TokenResponse:
    token = create_access_token({
        "user_id": user.id,
        "sub": user.username,
        "role": user.role,       # informational only - authorisation re-reads the database
        "team_id": user.team_id,
    })
    return TokenResponse(
        access_token=token,
        role=user.role,
        team_id=user.team_id,
        username=user.username,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def login_user(db: Session, request: LoginRequest) -> TokenResponse:
    try:
        user = authenticate_user(db, request.username, request.password)
    except AuthError as exc:
        logger.warning("login rejected user=%r status=%s", request.username[:64], exc.status_code)
        raise
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("login ok user=%s role=%s", user.username, user.role)
    return build_token_response(user)


def change_password(db: Session, user: User, current_password: str, new_password: str) -> None:
    """Change the authenticated user's password after verifying the current credential."""
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password_hash = hash_password(new_password)
    db.commit()


def get_user_from_token(db: Session, token: str) -> User:
    """Resolve a bearer token to its User row (does not check `active` - see dependencies)."""
    payload = decode_token_strict(token)
    user = db.query(User).filter(User.id == payload["user_id"]).first()
    if user is None:
        raise UserNotFoundError()
    return user


def user_has_role(user: User, *roles: str) -> bool:
    return user.role in roles


# ===========================================================================
# User management
# ===========================================================================
def _require_team(db: Session, team_id: Optional[int]) -> None:
    if team_id is not None and db.query(Team.id).filter(Team.id == team_id).first() is None:
        raise HTTPException(status_code=404, detail="Team not found")


def create_user(db: Session, data: UserCreate) -> User:
    """Create an account. Usernames are unique case-insensitively ('Coach1' and 'coach1' clash)."""
    identity_filters = [func.lower(User.username) == data.username.lower(),
                        func.lower(User.email) == data.username.lower()]
    if data.email:
        identity_filters.extend([
            func.lower(User.username) == data.email.lower(),
            func.lower(User.email) == data.email.lower(),
        ])
    if db.query(User.id).filter(or_(*identity_filters)).first() is not None:
        raise UserConflictError()

    player_id = data.player_id
    team_id = data.team_id
    if data.role == ROLE_PLAYER:
        player = db.query(Player).filter(Player.id == player_id).first()
        if player is None:
            raise HTTPException(status_code=404, detail="Player profile not found")
        if db.query(User.id).filter(User.player_id == player.id).first() is not None:
            raise UserConflictError("Player profile already has an account")
        if team_id is not None and team_id != player.team_id:
            raise HTTPException(status_code=400, detail="Player account team must match the player profile")
        team_id = player.team_id
    _require_team(db, team_id)

    user = User(
        username=data.username,
        password_hash=hash_password(data.password),
        role=data.role,
        team_id=team_id,
        player_id=player_id,
        full_name=data.full_name,
        email=data.email,
        phone=data.phone,
        active=data.active,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise UserConflictError()
    db.refresh(user)
    logger.info("user created id=%s username=%s role=%s active=%s", user.id, user.username, user.role, user.active)
    return user


def _active_admin_count(db: Session) -> int:
    return db.query(func.count(User.id)).filter(User.role == ROLE_ADMIN, User.active.is_(True)).scalar() or 0


def update_user(db: Session, user: User, data: UserUpdate) -> User:
    """Apply an admin edit. Refuses changes that would leave the system without an active admin."""
    changes = data.model_dump(exclude_unset=True)

    new_role = changes.get("role", user.role)
    new_active = changes.get("active", user.active)
    new_player_id = changes.get("player_id", user.player_id if new_role == ROLE_PLAYER else None)
    new_team = changes["team_id"] if "team_id" in changes else user.team_id

    if new_role not in ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")
    if new_role == ROLE_COACH and new_team is None:
        raise HTTPException(status_code=400, detail="A coach account must be assigned to a team")
    if new_role == ROLE_PLAYER:
        if new_player_id is None:
            raise HTTPException(status_code=400, detail="A player account must be linked to a player profile")
        player = db.query(Player).filter(Player.id == new_player_id).first()
        if player is None:
            raise HTTPException(status_code=404, detail="Player profile not found")
        linked_user = db.query(User.id).filter(User.player_id == new_player_id, User.id != user.id).first()
        if linked_user is not None:
            raise UserConflictError("Player profile already has an account")
        if "team_id" in changes and new_team != player.team_id:
            raise HTTPException(status_code=400, detail="Player account team must match the player profile")
        new_team = player.team_id
    elif new_player_id is not None:
        raise HTTPException(status_code=400, detail="Only a player account can be linked to a player profile")
    _require_team(db, new_team)

    losing_admin = user.role == ROLE_ADMIN and user.active and (new_role != ROLE_ADMIN or not new_active)
    if losing_admin and _active_admin_count(db) <= 1:
        raise HTTPException(status_code=409, detail="Cannot remove or disable the last active admin")

    if "full_name" in changes:
        user.full_name = changes["full_name"]
    if changes.get("password"):
        user.password_hash = hash_password(changes["password"])
    if "email" in changes:
        email = changes["email"]
        if email is not None and db.query(User.id).filter(
            User.id != user.id,
            or_(func.lower(User.username) == email.lower(),
                func.lower(User.email) == email.lower()),
        ).first() is not None:
            raise UserConflictError("Username or email already exists")
        user.email = changes["email"]
    if "phone" in changes:
        user.phone = changes["phone"]
    user.role = new_role
    user.team_id = new_team
    user.player_id = new_player_id
    user.active = new_active
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise UserConflictError("Username, email, or player profile already exists")
    db.refresh(user)
    logger.info("user updated id=%s username=%s role=%s active=%s", user.id, user.username, user.role, user.active)
    return user


def ensure_bootstrap_admin(db: Session) -> Optional[User]:
    """
    Create the first admin from BOOTSTRAP_ADMIN_USERNAME / BOOTSTRAP_ADMIN_PASSWORD when the
    database has no admin at all. Never changes an existing account or password.
    """
    if not settings.BOOTSTRAP_ADMIN_USERNAME or not settings.BOOTSTRAP_ADMIN_PASSWORD:
        return None
    if db.query(User.id).filter(User.role == ROLE_ADMIN).first() is not None:
        return None
    try:
        user = create_user(db, UserCreate(
            username=settings.BOOTSTRAP_ADMIN_USERNAME,
            password=settings.BOOTSTRAP_ADMIN_PASSWORD,
            role=ROLE_ADMIN,
        ))
    except (AuthError, ValueError) as exc:
        logger.error("bootstrap admin could not be created: %s", getattr(exc, "detail", exc))
        return None
    logger.warning("bootstrap admin '%s' created - remove BOOTSTRAP_ADMIN_PASSWORD from .env now", user.username)
    return user