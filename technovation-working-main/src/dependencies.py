"""
AthleteGuard / Technovation - reusable FastAPI dependencies.

    get_db                         one SQLAlchemy session per request (re-exported from database.py)
    get_current_user               valid bearer token -> User row (401 otherwise)
    get_current_active_user        same + the account must be active (403 otherwise)   <- use this on routes
    get_optional_user              user when a token is sent, None when there is none (bad token -> 401)
    require_roles(*roles)          role guard factory (roles are checked against the DATABASE row)
    require_admin / require_staff  ready-made guards (staff = admin + medical)
    require_service_or_roles(...)  AI pipeline gate: X-Service-Token header OR a user with an allowed role
    accessible_team_ids / can_access_team / ensure_team_access
                                   team scoping: admin + medical see every team, a coach only their own

Roles come from config.py (admin, coach, medical). No role system is invented here.
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from auth import (
    AuthError, ForbiddenError, InactiveUserError, InvalidTokenError, MissingTokenError,
    get_user_from_token,
)
from config import ROLE_ADMIN, ROLE_COACH, ROLE_PLAYER, STAFF_ROLES, settings
from database import get_db
from models import User

logger = logging.getLogger("athleteguard.deps")

# auto_error=False: a missing header becomes OUR 401 ("Not authenticated"), not FastAPI's 403/422.
bearer_scheme = HTTPBearer(auto_error=False, description="Paste the access_token returned by POST /auth/login")

__all__ = [
    "get_db", "bearer_scheme", "Principal",
    "get_current_user", "get_current_active_user", "get_optional_user",
    "require_roles", "require_admin", "require_staff", "require_service_or_roles",
    "accessible_team_ids", "can_access_team", "ensure_team_access",
]


# ===========================================================================
# Current user
# ===========================================================================
def _token_from(credentials: Optional[HTTPAuthorizationCredentials]) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials.strip():
        raise MissingTokenError()
    return credentials.credentials.strip()


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Valid, unexpired token whose user still exists. Does not check the active flag."""
    return get_user_from_token(db, _token_from(credentials))


def get_current_active_user(user: User = Depends(get_current_user)) -> User:
    """Current user, and the account must not be disabled."""
    if not user.active:
        raise InactiveUserError()
    if user.role == ROLE_PLAYER and (user.player is None or not user.player.active):
        raise InactiveUserError()
    return user


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """None when no Authorization header is sent. A header that IS sent must be valid."""
    if credentials is None:
        return None
    user = get_user_from_token(db, _token_from(credentials))
    if not user.active or (user.role == ROLE_PLAYER and (user.player is None or not user.player.active)):
        raise InactiveUserError()
    return user


# ===========================================================================
# Roles
# ===========================================================================
def require_roles(*roles: str, detail: Optional[str] = None) -> Callable[..., User]:
    """
    Dependency factory: the caller must be an active user whose role (from the database) is in `roles`.
        @router.get("/x", dependencies=[Depends(require_roles("admin"))])
        def x(user: User = Depends(require_roles("admin", "medical"))): ...
    """
    allowed = tuple(roles)

    def _checker(user: User = Depends(get_current_active_user)) -> User:
        if user.role not in allowed:
            raise ForbiddenError(detail or "Requires role: " + ", ".join(allowed))
        return user

    return _checker


require_admin = require_roles(ROLE_ADMIN, detail="Only admin can perform this action")
require_staff = require_roles(*STAFF_ROLES, detail="Only admin or medical staff can perform this action")


# ===========================================================================
# Team scoping
# ===========================================================================
def accessible_team_ids(user: User) -> Optional[List[int]]:
    """
    None  -> no restriction (admin and medical see every team).
    [..]  -> only these teams (a coach: their own team; a coach without a team sees nothing).
    """
    if user.role in STAFF_ROLES:
        return None
    if user.role in (ROLE_COACH, ROLE_PLAYER) and user.team_id is not None:
        return [user.team_id]
    return []


def can_access_team(user: User, team_id: Optional[int]) -> bool:
    allowed = accessible_team_ids(user)
    if allowed is None:
        return True
    return team_id is not None and team_id in allowed


def ensure_team_access(user: User, team_id: Optional[int], detail: str = "You can only access your own team") -> None:
    if not can_access_team(user, team_id):
        raise ForbiddenError(detail)


# ===========================================================================
# AI pipeline gate
# ===========================================================================
@dataclass(frozen=True)
class Principal:
    """Who is calling an AI-ingest endpoint."""
    kind: str                       # "service" | "user" | "anonymous"
    user: Optional[User] = None

    @property
    def label(self) -> str:
        if self.user is not None:
            return self.user.username
        return self.kind


_warned_anonymous = False


def require_service_or_roles(*roles: str, detail: Optional[str] = None) -> Callable[..., Principal]:
    """
    Gate for endpoints the AI pipeline calls (create match / event / collision, update, track upsert).

      1. X-Service-Token header equal to AI_SERVICE_TOKEN        -> Principal("service")
      2. otherwise a normal bearer token of an active user whose role is in `roles`
      3. no credentials at all:
           AI_INGEST_REQUIRE_AUTH=true  (default) -> 401
           AI_INGEST_REQUIRE_AUTH=false           -> Principal("anonymous")  (local demo only, logged once)

    A wrong service token is rejected outright - it never falls through to anonymous access.
    """
    allowed = tuple(roles)

    def _checker(
        x_service_token: Optional[str] = Header(default=None, alias="X-Service-Token"),
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
        db: Session = Depends(get_db),
    ) -> Principal:
        global _warned_anonymous

        if x_service_token is not None:
            expected = settings.AI_SERVICE_TOKEN
            if expected and hmac.compare_digest(x_service_token.encode("utf-8"), expected.encode("utf-8")):
                return Principal(kind="service")
            raise InvalidTokenError("Invalid service token")

        if credentials is not None:
            user = get_user_from_token(db, _token_from(credentials))
            if not user.active:
                raise InactiveUserError()
            if user.role not in allowed:
                raise ForbiddenError(detail or "Requires role: " + ", ".join(allowed))
            return Principal(kind="user", user=user)

        if not settings.AI_INGEST_REQUIRE_AUTH:
            if not _warned_anonymous:
                logger.warning("AI_INGEST_REQUIRE_AUTH=false: AI endpoints accept unauthenticated calls. "
                               "Use this on a local demo machine only.")
                _warned_anonymous = True
            return Principal(kind="anonymous")

        raise AuthError("Not authenticated: send X-Service-Token (AI pipeline) or a bearer token")

    return _checker