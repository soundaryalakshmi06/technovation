"""Non-diagnostic summaries derived only from already-recorded event metadata."""

from datetime import datetime, timezone
import re

from config import settings

DISCLAIMER = "AI assessment only — not a medical diagnosis. Professional medical evaluation is recommended."
_RISKS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def _format_region(region):
    if not region:
        return "Unspecified region"

    value = str(region).strip().upper().replace("_", " ").replace("-", " ")
    if not value:
        return "Unspecified region"
    if any(part in value for part in ("HEAD", "FACE", "EYE")) and "NECK" in value:
        return "Head / neck"
    if any(part in value for part in ("HEAD", "FACE", "EYE")):
        return "Head / face"
    if "NECK" in value:
        return "Neck"
    if any(part in value for part in ("CHEST", "TORSO")):
        return "Chest / torso"
    if any(part in value for part in ("SPINE", "BACK")):
        return "Spine / back"
    if any(part in value for part in ("KNEE", "LEG", "THIGH", "CALF", "ANKLE")):
        return "Knee / leg"
    if any(part in value for part in ("ARM", "ELBOW", "WRIST", "HAND", "SHOULDER")):
        return "Arm / shoulder"
    return value.title()


def _safe_player_name(event):
    player = getattr(event, "player", None)
    if player is None or getattr(player, "id", None) != getattr(event, "player_id", None):
        return "Player"
    return safe_display_player_name(
        getattr(player, "name", None),
        getattr(event, "player_id", None),
        getattr(event, "identified_by", None),
        getattr(event, "id_confidence", None),
    )


def sanitize_player_name(name):
    if not isinstance(name, str):
        return "Player"
    name = re.sub(r"\s+", " ", name).strip()
    if not name or re.fullmatch(r"(?:unknown(?:\s+player)?|unidentified(?:\s+player)?|player|player\s*#\s*\d+)", name, re.I):
        return "Player"
    return name


def safe_display_player_name(name, player_id, identified_by, confidence):
    reliable = (
        player_id is not None
        and (
            identified_by == "manual"
            or (
                confidence is not None
                and float(confidence) >= settings.IDENTITY_CONFIRMED_MIN_CONFIDENCE
            )
        )
    )
    return sanitize_player_name(name) if reliable else "Player"


def _risk_level(value):
    risk = str(value or "").strip().upper()
    return risk if risk in _RISKS else None


def _assessment_text(risk, region):
    region_phrase = (
        "an unspecified region"
        if region == "Unspecified region"
        else f"the recorded {region.lower()} region"
    )
    if risk == "CRITICAL":
        return (
            f"High-risk AI-detected incident involving {region_phrase}.",
            "High-risk AI-detected incident. Immediate professional medical evaluation is recommended.",
            True,
        )
    if risk == "HIGH":
        return (
            f"Higher-risk AI screening signal involving {region_phrase}.",
            "Prompt professional medical evaluation is recommended.",
            True,
        )
    if risk == "MEDIUM":
        return (
            f"Moderate AI screening signal involving {region_phrase}.",
            "Professional medical review is recommended based on the recorded signals.",
            True,
        )
    if risk == "LOW":
        return (
            f"Limited/low-risk visual screening signal involving {region_phrase}.",
            "No urgent escalation is suggested by this limited signal; seek professional evaluation if there is concern.",
            False,
        )
    return (
        "Risk level is not recorded; this assessment is limited to the available event metadata.",
        "Professional review is recommended if there is concern.",
        False,
    )


def build_medical_assessment(event=None, collision=None, event_clip_url=None, collision_clip_url=None,
                             created_at=None):
    """Build a structured assessment without adding or inferring clinical findings."""
    source = event if event is not None else collision
    if source is None:
        raise ValueError("an injury event or collision event is required")

    region = _format_region(getattr(source, "region", None))
    risk = _risk_level(getattr(source, "risk", None))
    summary, action, requires_attention = _assessment_text(risk, region)
    observed_signs = []
    event_type = str(getattr(source, "event_type", "") or "").strip().upper()
    if event_type == "FALL":
        observed_signs.append("A fall event is recorded.")
    elif event_type == "COLLISION":
        observed_signs.append("A player-contact event is recorded.")
    if region != "Unspecified region":
        observed_signs.append(f"The event record flags the {region.lower()} region.")
    if getattr(source, "movement", None) is not None:
        observed_signs.append("Movement evidence is recorded for this event.")

    linked_collision = collision or getattr(event, "collision_event", None)
    collision_id = getattr(linked_collision, "id", None)
    if linked_collision is not None:
        observed_signs.append("A related player-contact event is recorded.")
        contact_frames = getattr(linked_collision, "contact_frames", None)
        if contact_frames is not None and contact_frames >= 2:
            observed_signs.append("The related contact record spans multiple frames.")

    clip_url = event_clip_url if event is not None else collision_clip_url
    if event is not None and linked_collision is not None and not clip_url:
        clip_url = collision_clip_url
    frame_number = getattr(source, "frame_number", None)
    video_time_sec = getattr(source, "video_time_sec", None)
    evidence_available = bool(clip_url or frame_number is not None or video_time_sec is not None)
    if clip_url:
        observed_signs.append("An event video clip is available for review.")
    elif frame_number is not None or video_time_sec is not None:
        observed_signs.append("A video frame/time reference is recorded; no clip is attached.")
    else:
        observed_signs.append("No event frame or clip is attached; this assessment uses stored event metadata only.")

    return {
        "id": f"event-{event.id}" if event is not None else f"collision-{collision.id}",
        "injury_event_id": getattr(event, "id", None),
        "collision_event_id": collision_id or (getattr(collision, "id", None) if collision is not None else None),
        "player_id": getattr(event, "player_id", None) if event is not None else None,
        "player_name": _safe_player_name(event) if event is not None else "Player",
        "event_type": event_type or "INCIDENT",
        "affected_region": region,
        "risk_level": risk,
        "observed_signs": observed_signs,
        "assessment_summary": summary,
        "recommended_action": action,
        "requires_medical_attention": requires_attention,
        "confidence": getattr(source, "confidence", None),
        "evidence_available": evidence_available,
        "evidence_clip_url": clip_url,
        "frame_number": frame_number,
        "video_time_sec": video_time_sec,
        "collision_context": "Related player-contact event is recorded." if linked_collision is not None else None,
        "disclaimer": DISCLAIMER,
        "created_at": created_at or datetime.now(timezone.utc),
    }


def build_injury_note(event_type, region, risk):
    """Legacy event-note text, kept non-diagnostic for API/notification compatibility."""
    risk = _risk_level(risk)
    if (event_type or "").strip().upper() == "COLLISION":
        text = "A player-contact event is recorded."
    else:
        region_label = _format_region(region).lower()
        text, _, _ = _assessment_text(risk, region_label)
    return f"{text} {DISCLAIMER}"
