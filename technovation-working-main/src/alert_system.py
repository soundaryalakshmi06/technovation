import csv
import os
from datetime import datetime

import requests

from config import settings
from injury_notes import build_injury_note, safe_display_player_name
import whatsapp_notifier


# ============================================
# CONFIG
# ============================================

# Base URL of the active backend (src/main.py). Configurable via API_BASE_URL in .env.
API_BASE = settings.API_BASE_URL.rstrip("/")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
LOG_FILE = os.path.join(OUTPUT_DIR, "injury_alerts_v3.csv")


# How each identification method is shown in messages
METHOD_LABELS = {
    "jersey": "Jersey number",
    "jersey_name": "Name on shirt",
    "jersey_history": "Jersey number (frames before the fall)",
    "jersey_handoff": "Jersey number (tracked)",
    "face": "Face recognition",
    "manual": "Manual",
}


def describe_identification(identified_by, id_detail, id_confidence):

    if not identified_by:
        return "Not identified yet"

    text = METHOD_LABELS.get(identified_by, identified_by)

    if id_detail:
        text += " - " + str(id_detail)

    if id_confidence is not None:
        text += " (" + str(int(round(float(id_confidence) * 100))) + "%)"

    return text


def format_video_time(seconds):

    if seconds is None:
        return "-"

    seconds = int(seconds)

    return "%d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


# Which event types trigger a WhatsApp message.
# Collisions happen often in match footage, so only falls by default.
# Add "COLLISION" here if you want those too.
WHATSAPP_EVENT_TYPES = {"FALL"}

# Send a short follow-up message when an Unidentified fall gets its player filled in
SEND_IDENTIFIED_UPDATE = True

REQUEST_TIMEOUT = 3


# ============================================
# AUTHENTICATED REQUEST HELPER (AI ingest)
# ============================================
# Every write made by this module (create match / save event / update event / attach clip) is
# protected on the backend by require_service_or_roles(). This module authenticates with the
# shared AI service token sent in the X-Service-Token header.
#
# The token is read from configuration (AI_SERVICE_TOKEN, i.e. .env / environment). It is NEVER
# hardcoded here and never printed. When no token is configured the header is omitted, so the
# previous behaviour (401 unless a bearer token is supplied) is preserved.

def _ai_ingest_headers(extra=None):
    headers = dict(extra or {})
    token = (settings.AI_SERVICE_TOKEN or "").strip()
    if token:
        headers["X-Service-Token"] = token
    return headers


def ai_request(method, path, **kwargs):
    """Send one authenticated request to the active backend's AI-ingest API."""
    headers = _ai_ingest_headers(kwargs.pop("headers", None))
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    return requests.request(method, API_BASE + path, headers=headers, **kwargs)


# ============================================
# WHATSAPP LIFECYCLE
# ============================================

def start_notifications():
    """Opens WhatsApp Web early so the QR scan can happen before detection starts."""
    if len(WHATSAPP_EVENT_TYPES) > 0:
        whatsapp_notifier.start()


def finish():
    """Waits for queued WhatsApp messages to be sent, then closes Chrome."""
    whatsapp_notifier.shutdown()


# ============================================
# MATCH SESSION
# ============================================

def create_match(name, video_source):

    try:
        response = ai_request(
            "POST",
            "/matches",
            json={"name": name, "video_source": str(video_source)},
        )

        if response.status_code == 200:
            match_id = response.json()["match_session_id"]
            print("[API] Match session created:", match_id, "-", name)
            return match_id

        print("[API] Failed to create match:", response.status_code, response.text)

    except requests.exceptions.RequestException as e:
        print("[API] Could not reach backend to create match:", e)

    return None


# ============================================
# CSV LOG (local backup of every event)
# ============================================

def _write_csv(row):

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    new_file = not os.path.exists(LOG_FILE)

    with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:

        writer = csv.writer(file)

        if new_file:
            writer.writerow([
                "Date", "Time", "Match", "Video Time", "Event ID", "Track ID", "Player",
                "Identified By", "Event", "Body Region", "Movement", "Risk Level", "Injury Note"
            ])

        writer.writerow(row)


# ============================================
# SAVE EVENT
# Always saved immediately — player may be None
# (Unidentified) and filled in later with
# update_event_player(). Returns the event id
# from the backend, or None if it couldn't post.
# ============================================

def save_event(
    event_type,
    region,
    movement,
    risk,
    track_id,
    player_id=None,
    player_name=None,
    jersey_number=None,
    match_id=None,
    match_name=None,
    identified_by=None,
    id_detail=None,
    id_confidence=None,
    video_time_sec=None,
):

    injury_note = build_injury_note(event_type, region, risk)

    movement = round(float(movement or 0), 2)

    now = datetime.now()

    payload = {
        "player_id": player_id,
        "match_session_id": match_id,
        "event_type": event_type,
        "region": region,
        "risk": risk,
        "movement": movement,
        "injury_note": injury_note,
        "source": "ai",
        "track_id": track_id,
        "video_time_sec": video_time_sec,
        "identified_by": identified_by if player_id is not None else None,
        "id_detail": id_detail if player_id is not None else None,
        "id_confidence": id_confidence if player_id is not None else None,
    }

    event_id = None

    try:
        response = ai_request("POST", "/events", json=payload)

        if response.status_code in (200, 201):
            event_id = response.json()["event_id"]
        else:
            print("[API] Failed to post event:", response.status_code, response.text)

    except requests.exceptions.RequestException as e:
        print("[API] Could not reach backend:", e)

    display_player = safe_display_player_name(player_name, player_id, identified_by, id_confidence)

    print()
    print("====================================")
    print("       🚨 EVENT SAVED 🚨")
    print("====================================")
    print("EVENT ID:", event_id)
    print("PLAYER:", display_player)
    print("IDENTIFIED BY:", describe_identification(identified_by, id_detail, id_confidence))
    print("VIDEO TIME:", format_video_time(video_time_sec))
    print("EVENT:", event_type)
    print("REGION:", region)
    print("MOVEMENT:", movement)
    print("RISK:", risk)
    print("NOTE:", injury_note)
    print("TIME:", now.strftime("%Y-%m-%d %H:%M:%S"))
    print("====================================")

    _write_csv([
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M:%S"),
        match_name or "",
        format_video_time(video_time_sec),
        event_id if event_id is not None else "",
        track_id,
        display_player,
        describe_identification(identified_by, id_detail, id_confidence),
        event_type,
        region,
        movement,
        risk,
        injury_note,
    ])

    if event_type in WHATSAPP_EVENT_TYPES:

        message = whatsapp_notifier.build_alert_message(
            player_name=display_player,
            jersey_number=None,
            event_type=event_type,
            region=region,
            risk=risk,
            injury_note=injury_note,
            match_name=match_name,
            when_text=now.strftime("%d-%m-%Y %I:%M:%S %p"),
            player_id=player_id,
            identified_by=identified_by,
            id_confidence=id_confidence,
        )

        message += "\nVideo time: " + format_video_time(video_time_sec)

        whatsapp_notifier.send_message(message)

    return event_id


# ============================================
# FILL IN THE PLAYER ON AN EXISTING EVENT
# ============================================

def save_collision(
    match_id,
    track_ids,
    participants,
    video_time_sec,
    frame_number,
    risk,
    iou=None,
    contact_frames=None,
    location_x=None,
    location_y=None,
):
    """Persist a collision and its two track-specific identities."""
    payload = {
        "match_session_id": match_id,
        "frame_number": frame_number,
        "video_time_sec": video_time_sec,
        "risk": risk,
        "iou": iou,
        "contact_frames": contact_frames,
        "location_x": location_x,
        "location_y": location_y,
        "source": "ai",
        "note": "Sustained tracked-player overlap; screening signal, not a diagnosis.",
        "participants": [
            {
                "slot": slot,
                "track_id": track_id,
                "player_id": identity.get("player_id") if identity else None,
                "identified_by": identity.get("method") if identity else None,
                "id_detail": identity.get("detail") if identity else None,
                "id_confidence": identity.get("confidence") if identity else None,
                "region": participant.get("region"),
                "movement": participant.get("movement"),
            }
            for slot, track_id, identity, participant in participants
        ],
    }

    try:
        response = ai_request("POST", "/collisions", json=payload)
        if response.status_code in (200, 201):
            collision_id = response.json()["collision_event_id"]
            print("[API] Collision saved:", collision_id, "tracks", tuple(track_ids))
            return collision_id
        print("[API] Failed to post collision:", response.status_code, response.text)
    except requests.exceptions.RequestException as e:
        print("[API] Could not reach backend to save collision:", e)

    return None


def update_event_player(event_id, player_id, player_name, jersey_number=None,
                        event_type=None, match_name=None,
                        identified_by=None, id_detail=None, id_confidence=None):

    if event_id is None:
        return False

    try:
        response = ai_request(
            "PATCH",
            "/events/" + str(event_id) + "/ai-update",
            json={
                "player_id": player_id,
                "identified_by": identified_by,
                "id_detail": id_detail,
                "id_confidence": id_confidence,
            },
        )

        if response.status_code != 200:
            print("[API] Failed to update player on event", event_id, ":", response.status_code, response.text)
            return False

    except requests.exceptions.RequestException as e:
        print("[API] Could not reach backend to update event", event_id, ":", e)
        return False

    display_player = safe_display_player_name(player_name, player_id, identified_by, id_confidence)
    print("[API] Event", event_id, "identity updated for", display_player)

    if SEND_IDENTIFIED_UPDATE and event_type in WHATSAPP_EVENT_TYPES:

        text = "ℹ️ Update: " + display_player + " is associated with the earlier " + str(event_type).lower() + " event."

        if match_name:
            text += "\nMatch: " + match_name

        whatsapp_notifier.send_message(text)

    return True


# ============================================
# ATTACH A CLIP TO AN EXISTING EVENT
# clip_filename = file name inside output\clips
# ============================================

def attach_clip(event_id, clip_filename):

    if event_id is None:
        return False

    try:
        response = ai_request(
            "PATCH",
            "/events/" + str(event_id) + "/ai-update",
            json={"clip_path": clip_filename},
        )

        if response.status_code == 200:
            print("[API] Clip attached to event", event_id, "->", clip_filename)
            return True

        print("[API] Failed to attach clip to event", event_id, ":", response.status_code, response.text)

    except requests.exceptions.RequestException as e:
        print("[API] Could not reach backend to attach clip:", e)

    return False


def attach_collision_clip(collision_id, clip_filename):
    """Attach a generated clip to its collision record."""
    if collision_id is None:
        return False

    try:
        response = ai_request(
            "PATCH",
            "/collisions/" + str(collision_id),
            json={"clip_path": clip_filename},
        )
        if response.status_code == 200:
            print("[API] Clip attached to collision", collision_id, "->", clip_filename)
            return True
        print("[API] Failed to attach clip to collision", collision_id, ":", response.status_code, response.text)
    except requests.exceptions.RequestException as e:
        print("[API] Could not reach backend to attach collision clip:", e)

    return False