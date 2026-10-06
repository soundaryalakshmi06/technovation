import cv2
import math
import csv
import os
import sys
import threading
import unicodedata
from collections import deque
from datetime import datetime

import torch
from ultralytics import YOLO

from alert_system import (
    save_event,
    save_collision,
    update_event_player,
    attach_clip,
    attach_collision_clip,
    create_match,
    start_notifications,
    finish as finish_notifications,
)
from player_identifier import PlayerIdentifier

# START

print("====================================")
print(" REAL-TIME INJURY DETECTION v2 (FIFA/BROADCAST)")
print("====================================")

# ============================================================
# DEVICE + AI MODEL CONFIGURATION
# ============================================================

# No NVIDIA GPU on this machine.
# Run YOLO11-Pose on CPU.
DEVICE = "cpu"

# Lightweight YOLO11 Pose model for CPU.
POSE_MODEL = "yolo11n-pose.pt"

# CPU starting resolution.
INFER_IMGSZ = 640

# BoT-SORT tracker configuration
TRACKER_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "tracker_botsort_reid.yaml"
)

print("====================================")
print(" AI MODEL CONFIGURATION")
print("====================================")
print("Pose model :", POSE_MODEL)
print("Image size :", INFER_IMGSZ)
print("Device     :", DEVICE.upper())
print("Tracker    :", os.path.basename(TRACKER_CONFIG))
print("====================================")

# Load YOLO pose model
try:
    model = YOLO(POSE_MODEL)
    print("[AI] YOLO11n-Pose loaded successfully")
except Exception as e:
    print("[AI ERROR] Could not load pose model:", e)
    sys.exit(1)

if not os.path.isfile(TRACKER_CONFIG):
    print(
        "[WARNING] Tracker config not found at",
        TRACKER_CONFIG,
        "- using default tracker"
    )
    TRACKER_CONFIG = "botsort.yaml"

print("[AI] Model ready")
print("[AI] Tracker:", os.path.basename(TRACKER_CONFIG))
print("====================================")

# ============================================================
# VIDEO SOURCE CONFIG
# ============================================================

SOURCE_MODE = "file"

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

DEFAULT_VIDEO = os.path.join(
    PROJECT_ROOT,
    "videos",
    "fall_test.mp4"
)

VIDEO_FILE = (
    sys.argv[1]
    if len(sys.argv) > 1
    else DEFAULT_VIDEO
)

MATCH_NAME = (
    sys.argv[2]
    if len(sys.argv) > 2
    else None
)

OUTPUT_DIR = os.path.join(
    PROJECT_ROOT,
    "output"
)

CLIPS_DIR = os.path.join(
    OUTPUT_DIR,
    "clips"
)

os.makedirs(CLIPS_DIR, exist_ok=True)

SOURCE_CONFIG = {
    "file": VIDEO_FILE,
    "webcam": 0,
    "capture_card": 1,
}

TARGET_WIDTH = 1920
TARGET_HEIGHT = 1080
TARGET_FPS = 30

# v2 FEATURE SETTINGS

ENABLE_IDENTIFICATION = True     # jersey number -> name on shirt -> face
ENABLE_FACE = True               # needs photos in <project root>\known_players (see check_face_gallery.py)

ENABLE_COLLISION_ALERTS = False  # False = falls only, collisions are ignored

IDENTIFY_WINDOW_SECONDS = 120     # how long an Unidentified fall waits for a name

CLIP_PRE_SECONDS = 3             # seconds of video kept BEFORE the fall
CLIP_POST_SECONDS = 3            # seconds recorded AFTER the fall
CLIP_WIDTH = 854                 # clips are downscaled to this width (saves memory)

FALL_COOLDOWN_SECONDS = 5        # same player can trigger a new fall after this
COLLISION_COOLDOWN_SECONDS = 5   # same pair can trigger a new collision after this

# ---- one fall = one event, even when the tracker renumbers the player ----
INCIDENT_MERGE_SECONDS = 3.0     # falls this close in time ...
INCIDENT_MERGE_DISTANCE = 1.2    # ... and this close in space (x box size) are the same incident

# ---- carry identity from the track that disappeared where the faller appeared ----
LINEAGE_MAX_GAP_SECONDS = 2.0    # old track must have vanished at most this long before
LINEAGE_MAX_DISTANCE = 1.5       # ... and this close (x box size) to where the new track appeared

# ---- camera cuts / replays ----
SCENE_CUT_THRESHOLD = 0.55       # histogram correlation below this = camera cut

# ---- follow an Unidentified faller forward in time ----
# After a fall the player gets up, often under ANOTHER new track number, and is
# identified a few seconds later. That later identity is carried back to the event.
FOLLOW_FORWARD_EVERY_N_FRAMES = 15
FOLLOW_FORWARD_MAX_DEPTH = 4

# ---- only detect falls during actual play ----
# Broadcasts cut to coaches, crowd, benches and face close-ups. Those shots show
# little or no pitch, and the person fills the screen with no legs visible.
MIN_PITCH_GREEN_FRACTION = 0.25  # frame must be at least this much grass to count as gameplay
MAX_BOX_HEIGHT_FRACTION = 0.55   # a person taller than this share of the frame = close-up
REQUIRE_LEGS_FOR_FALL = True     # knees or ankles must be visible to judge a fall


def open_video_source():
    source = SOURCE_CONFIG[SOURCE_MODE]
    if SOURCE_MODE == "file" and not os.path.isfile(source):
        print("ERROR: Video file not found:", source)
        print("Place the video there, or run:  python multi_player_injury.py \"<path to video>\"")
        sys.exit(1)
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print("ERROR: Video source cannot be opened:", source)
        sys.exit(1)
    if SOURCE_MODE in ("webcam", "capture_card"):
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, TARGET_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, TARGET_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)
    print("Video source opened successfully:", SOURCE_MODE, "->", source)
    return cap


cap = open_video_source()

fps = cap.get(cv2.CAP_PROP_FPS)
total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

EFFECTIVE_FPS = fps if fps and fps > 0 else 30

print("FPS:", round(EFFECTIVE_FPS, 2), "| Total frames:", total_frames)

# MATCH SESSION — one per run

if MATCH_NAME is None:
    stamp = datetime.now().strftime("%d %b %Y %H:%M")
    if SOURCE_MODE == "file":
        MATCH_NAME = os.path.splitext(os.path.basename(VIDEO_FILE))[0] + " - " + stamp
    else:
        MATCH_NAME = SOURCE_MODE.replace("_", " ").title() + " - " + stamp

match_id = create_match(MATCH_NAME, SOURCE_CONFIG[SOURCE_MODE])

# PLAYER IDENTIFICATION

identifier = None

if ENABLE_IDENTIFICATION:
    try:
        identifier = PlayerIdentifier(enable_face=ENABLE_FACE, jersey_max_attempts=400, min_box_size=70)
    except Exception as e:
        print("[WARNING] Player identification disabled:", e)
        identifier = None

if identifier is not None:
    print("[ID] Jersey number reading: ON")
    if identifier.face_ready:
        print("[ID] Face recognition: ON")
    else:
        print("[ID] Face recognition: waiting - it switches on automatically once a player's face")
        print("     is learned from a clear jersey read (no photo uploads needed)")

# Opens WhatsApp Web now so you can scan the QR code while detection runs
start_notifications()

print("Starting video loop...")
print("Press Q to quit")

# VARIABLES

player_history = {}
player_status = {}
player_region = {}
player_risk = {}
player_movement = {}

last_fall_frame = {}          # track_id -> frame of last saved fall
last_collision_frame = {}     # (id_a, id_b) -> frame of last saved collision
track_identity = {}           # track_id -> identifier result dict
pending_identification = {}   # event_id -> {"tracks", "frame", "event_type"}

face_announced = identifier is not None and identifier.face_ready

track_first_seen = {}         # track_id -> (frame, box) when first detected
track_last_seen = {}          # track_id -> (frame, box) last detected (any box, valid or not)
ever_upright = {}             # track_id -> True once seen standing

recent_incidents = []         # [{"event_id", "frame", "box", "tracks", "identified"}]

scene_cut_frames = []         # frames where a camera cut was detected
prev_scene_hist = None

stats_skipped_no_upright = 0
stats_merged_falls = 0
stats_inherited = 0
stats_followed_forward = 0
stats_non_gameplay_frames = 0

frame_count = 0

# CALIBRATION LOGGING (for threshold tuning)

calibration_log = open(os.path.join(OUTPUT_DIR, "calibration_log.csv"), "w", newline="")
calibration_writer = csv.writer(calibration_log)
calibration_writer.writerow([
    "frame", "time_sec", "track_id", "downward_ratio", "angle",
    "torso_angle", "aspect_ratio", "is_horizontal", "fall_movement_fired",
    "posture_fired", "ground_truth"
])

# EVENT LOG (for end-of-video summary)
event_log = []

# DEBUG SETTINGS
DEBUG_FALL_METRICS = True

# PLAYER VALIDATION SETTINGS
PERSON_CONFIDENCE = 0.45
KEYPOINT_CONFIDENCE = 0.35
MIN_KEYPOINTS = 5
MIN_BOX_HEIGHT = 35
CONFIRM_FRAMES = 2            # v1: 3 — players confirmed faster
CONTINUE_CONFIDENCE = 0.30

# FALL DETECTION SETTINGS (MOVEMENT-BASED)
FALL_DOWNWARD_RATIO = 0.18
FALL_ANGLE_THRESHOLD = 60

# FALL DETECTION SETTINGS (STATIC POSTURE-BASED)
STATIC_FALL_ANGLE_THRESHOLD = 55
ASPECT_RATIO_THRESHOLD = 1.2
POSTURE_CONFIRM_FRAMES = 1    # v1: 2 — quick falls are no longer missed

posture_streak = {}
ground_state = {}

# COLLISION DETECTION SETTINGS
COLLISION_IOU_THRESHOLD = 0.20
COLLISION_CONFIRM_FRAMES = 2

collision_streak = {}
active_collisions = set()

# TRACKING STATE FOR VALIDATION
valid_streak = {}
confirmed_players = set()

# COCO KEYPOINT INDEX REFERENCE
NOSE = 0
LEFT_EYE = 1
RIGHT_EYE = 2
LEFT_EAR = 3
RIGHT_EAR = 4
LEFT_SHOULDER = 5
RIGHT_SHOULDER = 6
LEFT_ELBOW = 7
RIGHT_ELBOW = 8
LEFT_WRIST = 9
RIGHT_WRIST = 10
LEFT_HIP = 11
RIGHT_HIP = 12
LEFT_KNEE = 13
RIGHT_KNEE = 14
LEFT_ANKLE = 15
RIGHT_ANKLE = 16

# GROUPED BODY REGIONS (v2)
# Nearby joints are grouped, because pose keypoints can't reliably separate
# knee from ankle or elbow from wrist on broadcast footage.
# NECK is derived from the shoulders and handled separately.
REGION_GROUPS = {
    "HEAD": [NOSE, LEFT_EYE, RIGHT_EYE, LEFT_EAR, RIGHT_EAR],
    "TORSO": [LEFT_SHOULDER, RIGHT_SHOULDER, LEFT_HIP, RIGHT_HIP],
    "LEFT ARM": [LEFT_ELBOW, LEFT_WRIST],
    "RIGHT ARM": [RIGHT_ELBOW, RIGHT_WRIST],
    "LEFT LEG": [LEFT_KNEE, LEFT_ANKLE],
    "RIGHT LEG": [RIGHT_KNEE, RIGHT_ANKLE],
}

# STANDING-PROPORTION BASELINE (per keypoint)
# Fraction of box height (from the top) where each keypoint sits on a
# normally standing person. Estimates — revisit with graded footage.
KEYPOINT_BASELINE = {
    NOSE: 0.05,
    LEFT_EYE: 0.04,
    RIGHT_EYE: 0.04,
    LEFT_EAR: 0.05,
    RIGHT_EAR: 0.05,
    LEFT_SHOULDER: 0.15,
    RIGHT_SHOULDER: 0.15,
    LEFT_ELBOW: 0.40,
    RIGHT_ELBOW: 0.40,
    LEFT_WRIST: 0.55,
    RIGHT_WRIST: 0.55,
    LEFT_HIP: 0.52,
    RIGHT_HIP: 0.52,
    LEFT_KNEE: 0.75,
    RIGHT_KNEE: 0.75,
    LEFT_ANKLE: 0.95,
    RIGHT_ANKLE: 0.95,
}

NECK_BASELINE = 0.12


# DERIVED POINT: NECK
def get_neck(points, conf):
    left_shoulder = points[LEFT_SHOULDER]
    right_shoulder = points[RIGHT_SHOULDER]
    left_conf = float(conf[LEFT_SHOULDER])
    right_conf = float(conf[RIGHT_SHOULDER])
    x = (left_shoulder[0] + right_shoulder[0]) / 2
    y = (left_shoulder[1] + right_shoulder[1]) / 2
    neck_conf = min(left_conf, right_conf)
    return (x, y), neck_conf


# LOWER-BODY ANCHOR FOR FALL ANGLE / MOVEMENT
def get_lower_anchor(points, conf):
    ankle_ok = (
        float(conf[LEFT_ANKLE]) >= KEYPOINT_CONFIDENCE
        and float(conf[RIGHT_ANKLE]) >= KEYPOINT_CONFIDENCE
    )
    if ankle_ok:
        left = points[LEFT_ANKLE]
        right = points[RIGHT_ANKLE]
        return ((left[0] + right[0]) / 2, (left[1] + right[1]) / 2)
    knee_ok = (
        float(conf[LEFT_KNEE]) >= KEYPOINT_CONFIDENCE
        and float(conf[RIGHT_KNEE]) >= KEYPOINT_CONFIDENCE
    )
    if knee_ok:
        left = points[LEFT_KNEE]
        right = points[RIGHT_KNEE]
        return ((left[0] + right[0]) / 2, (left[1] + right[1]) / 2)
    hip_ok = (
        float(conf[LEFT_HIP]) >= KEYPOINT_CONFIDENCE
        and float(conf[RIGHT_HIP]) >= KEYPOINT_CONFIDENCE
    )
    if hip_ok:
        left = points[LEFT_HIP]
        right = points[RIGHT_HIP]
        return ((left[0] + right[0]) / 2, (left[1] + right[1]) / 2)
    return None


# STRICT PLAYER VALIDATION
def is_strict_valid(box_confidence, points, conf, box_height):
    if box_confidence < PERSON_CONFIDENCE:
        return False
    if box_height < MIN_BOX_HEIGHT:
        return False
    valid_keypoints = 0
    for i in range(len(points)):
        kp_conf = float(conf[i])
        if kp_conf >= KEYPOINT_CONFIDENCE:
            x = float(points[i][0])
            y = float(points[i][1])
            if x > 0 and y > 0:
                valid_keypoints += 1
    if valid_keypoints < MIN_KEYPOINTS:
        if DEBUG_FALL_METRICS:
            print("[VALIDATE FAIL] valid_keypoints too low:", valid_keypoints)
        return False
    if (
        float(conf[LEFT_SHOULDER]) < KEYPOINT_CONFIDENCE
        or float(conf[RIGHT_SHOULDER]) < KEYPOINT_CONFIDENCE
    ):
        if DEBUG_FALL_METRICS:
            print("[VALIDATE FAIL] shoulders low conf:", float(conf[LEFT_SHOULDER]), float(conf[RIGHT_SHOULDER]))
        return False
    if get_lower_anchor(points, conf) is None:
        if DEBUG_FALL_METRICS:
            print("[VALIDATE FAIL] no lower anchor (ankle/knee/hip all low conf)")
        return False
    return True


# FALL METRICS (MOVEMENT-BASED)
def compute_fall_metrics(history):
    if len(history) < 2:
        return None, None
    previous = history[-2]
    current = history[-1]
    if (
        previous["lower_anchor"] is None
        or current["lower_anchor"] is None
    ):
        return None, None
    neck_prev = previous["neck"]
    neck_cur = current["neck"]
    anchor_cur = current["lower_anchor"]
    box_height = current["box_height"]
    if box_height <= 0:
        box_height = 1
    downward_movement = neck_cur[1] - neck_prev[1]
    downward_ratio = downward_movement / box_height
    dx = anchor_cur[0] - neck_cur[0]
    dy = anchor_cur[1] - neck_cur[1]
    angle = math.degrees(
        math.atan2(abs(dx), abs(dy))
    )
    return downward_ratio, angle


def detect_fall(history):
    downward_ratio, angle = compute_fall_metrics(history)
    if downward_ratio is None:
        return False
    if (
        downward_ratio > FALL_DOWNWARD_RATIO
        and angle > FALL_ANGLE_THRESHOLD
    ):
        return True
    return False


# STATIC POSTURE CHECK
def compute_static_posture(points, conf, box_width, box_height):
    shoulder_ok = (
        float(conf[LEFT_SHOULDER]) >= KEYPOINT_CONFIDENCE
        and float(conf[RIGHT_SHOULDER]) >= KEYPOINT_CONFIDENCE
    )
    if not shoulder_ok:
        return False, None
    neck, _ = get_neck(points, conf)
    lower_anchor = get_lower_anchor(points, conf)
    torso_angle = None
    if lower_anchor is not None:
        dx = neck[0] - lower_anchor[0]
        dy = neck[1] - lower_anchor[1]
        torso_angle = math.degrees(math.atan2(abs(dx), abs(dy)))
    aspect_ratio = 0
    if box_height > 0:
        aspect_ratio = box_width / box_height
    angle_horizontal = (
        torso_angle is not None
        and torso_angle > STATIC_FALL_ANGLE_THRESHOLD
    )
    ratio_horizontal = aspect_ratio > ASPECT_RATIO_THRESHOLD
    # Both signals must agree — a player reaching sideways can widen
    # the box without actually being horizontal.
    is_horizontal = angle_horizontal and ratio_horizontal
    return is_horizontal, torso_angle


# SINGLE-FRAME REGION ESTIMATE (grouped)
# Picks the region that has dropped furthest below its normal standing position.
def estimate_posture_region(points, conf, box_top, box_height):
    if box_height <= 0:
        return "TORSO", 0
    region_deviation = {}
    for region, indices in REGION_GROUPS.items():
        deviations = []
        for idx in indices:
            if float(conf[idx]) >= KEYPOINT_CONFIDENCE:
                y = float(points[idx][1])
                if y > 0:
                    normalized_y = (y - box_top) / box_height
                    deviations.append(normalized_y - KEYPOINT_BASELINE[idx])
        if len(deviations) > 0:
            region_deviation[region] = sum(deviations) / len(deviations)
    if (
        float(conf[LEFT_SHOULDER]) >= KEYPOINT_CONFIDENCE
        and float(conf[RIGHT_SHOULDER]) >= KEYPOINT_CONFIDENCE
    ):
        neck, _ = get_neck(points, conf)
        region_deviation["NECK"] = (float(neck[1]) - box_top) / box_height - NECK_BASELINE
    if len(region_deviation) == 0:
        return "TORSO", 0
    grounded_region = max(region_deviation, key=region_deviation.get)
    return grounded_region, 0


# BODY REGION MOVEMENT (history-based, grouped)
def _keypoint_distance(previous, current, index):
    if (
        previous["conf"][index] < KEYPOINT_CONFIDENCE
        or current["conf"][index] < KEYPOINT_CONFIDENCE
    ):
        return None
    dx = current["points"][index][0] - previous["points"][index][0]
    dy = current["points"][index][1] - previous["points"][index][1]
    distance = math.sqrt(dx * dx + dy * dy)
    box_height = current["box_height"]
    if box_height > 0:
        distance = (distance / box_height) * 100
    return distance


def calculate_region_movement(history):
    region_movements = {}
    # ---- NECK (derived) ----
    neck_movements = []
    for i in range(1, len(history)):
        previous = history[i - 1]
        current = history[i]
        dx = current["neck"][0] - previous["neck"][0]
        dy = current["neck"][1] - previous["neck"][1]
        distance = math.sqrt(dx * dx + dy * dy)
        box_height = current["box_height"]
        if box_height > 0:
            distance = (distance / box_height) * 100
        neck_movements.append(distance)
    if len(neck_movements) > 0:
        region_movements["NECK"] = sum(neck_movements) / len(neck_movements)
    # ---- GROUPED REGIONS ----
    for region, indices in REGION_GROUPS.items():
        frame_movements = []
        for i in range(1, len(history)):
            distances = []
            for idx in indices:
                d = _keypoint_distance(history[i - 1], history[i], idx)
                if d is not None:
                    distances.append(d)
            if len(distances) > 0:
                frame_movements.append(sum(distances) / len(distances))
        if len(frame_movements) > 0:
            region_movements[region] = sum(frame_movements) / len(frame_movements)
    if len(region_movements) == 0:
        return "UNKNOWN", 0
    suspected_region = max(region_movements, key=region_movements.get)
    movement = region_movements[suspected_region]
    return suspected_region, movement


# RISK CALCULATION (MOVEMENT-BASED)
def calculate_risk(movement):
    if movement < 12:
        return "LOW"
    elif movement < 22:
        return "MEDIUM"
    else:
        return "HIGH"


# RISK CALCULATION (STATIC POSTURE-BASED)
def calculate_posture_risk(torso_angle, aspect_ratio):
    angle_score = 0.0
    if torso_angle is not None:
        angle_score = (torso_angle - STATIC_FALL_ANGLE_THRESHOLD) / (90 - STATIC_FALL_ANGLE_THRESHOLD)
        angle_score = max(0.0, min(1.0, angle_score))
    ratio_score = 0.0
    if aspect_ratio > 0:
        ratio_score = (aspect_ratio - ASPECT_RATIO_THRESHOLD) / (2.4 - ASPECT_RATIO_THRESHOLD)
        ratio_score = max(0.0, min(1.0, ratio_score))
    severity = (angle_score + ratio_score) / 2
    if severity < 0.35:
        return "LOW"
    else:
        # Single-frame estimate has no motion history — cap at MEDIUM.
        return "MEDIUM"


def _head_to_lower_anchor_gap(points, conf, box_height):
    """Return an image-space head/neck-to-lower-body alignment proxy, when keypoints are reliable."""
    if box_height <= 0:
        return None
    neck, neck_conf = get_neck(points, conf)
    lower_anchor = get_lower_anchor(points, conf)
    if neck_conf < KEYPOINT_CONFIDENCE or lower_anchor is None:
        return None
    return abs(float(neck[1]) - float(lower_anchor[1])) / box_height


def _current_downward_ratio(history, points, conf, box_height):
    """Measure the current neck drop from the last reliable frame without changing fall detection."""
    if not history or box_height <= 0:
        return None
    if (
        min(float(conf[LEFT_SHOULDER]), float(conf[RIGHT_SHOULDER])) < KEYPOINT_CONFIDENCE
        or min(
            float(history[-1]["conf"][LEFT_SHOULDER]),
            float(history[-1]["conf"][RIGHT_SHOULDER]),
        ) < KEYPOINT_CONFIDENCE
    ):
        return None
    current_neck, _ = get_neck(points, conf)
    previous_neck = history[-1]["neck"]
    if previous_neck is None:
        return None
    return (float(current_neck[1]) - float(previous_neck[1])) / box_height


def active_contact_frames(track_id):
    """Return confirmed overlap duration involving this track, or zero."""
    return max(
        (collision_streak.get(pair, 0) for pair in active_collisions if track_id in pair),
        default=0,
    )


def calculate_injury_risk(base_risk, region, movement, downward_ratio, torso_angle,
                          aspect_ratio, head_anchor_gap, posture_frames,
                          observed_standing, contact_frames=0):
    """Combine the existing fall signals with body-region priority; this is screening, not diagnosis."""
    region_text = str(region or "").upper().replace("/", " ").replace("_", " ").replace("-", " ")
    region_words = set(region_text.split())
    cranial = bool(region_words & {"HEAD", "FACE", "EYE", "EYES", "NECK"})
    core = bool(region_words & {"TORSO", "CHEST", "SPINE", "BACK"})
    high_priority_limb = (
        bool(region_words & {"KNEE", "LEG", "SHOULDER", "HIP"})
        or "LEG" in region_words
    )
    low_priority_limb = bool(region_words & {"ARM", "ELBOW", "WRIST", "HAND", "THIGH", "CALF", "ANKLE"})
    if "ARM" in region_words or "LEG" in region_words:
        low_priority_limb = low_priority_limb or not high_priority_limb

    strong_movement = movement >= 22
    moderate_movement = movement >= 12
    sudden_drop = downward_ratio is not None and downward_ratio >= FALL_DOWNWARD_RATIO
    very_sudden_drop = downward_ratio is not None and downward_ratio >= FALL_DOWNWARD_RATIO * 1.5
    horizontal = (
        torso_angle is not None
        and torso_angle > STATIC_FALL_ANGLE_THRESHOLD
        and aspect_ratio > ASPECT_RATIO_THRESHOLD
    )
    head_alignment = head_anchor_gap is not None and head_anchor_gap <= 0.35
    contact = contact_frames >= COLLISION_CONFIRM_FRAMES
    strong_contact = contact_frames >= COLLISION_CONFIRM_FRAMES * 2
    persistent_posture = horizontal and posture_frames >= 3
    prolonged_posture = horizontal and posture_frames >= 6

    general_evidence_count = sum((
        strong_movement, sudden_drop, horizontal, contact,
        bool(observed_standing), persistent_posture,
    ))
    cranial_evidence_count = general_evidence_count + int(head_alignment)

    # Head/face/eye/neck labels alone do not elevate risk. HIGH requires
    # converging fall/impact evidence; CRITICAL requires several signals plus
    # strong contact or a very sudden drop and evidence the player had stood up.
    if cranial:
        critical_supported = (
            cranial_evidence_count >= 6
            and bool(observed_standing)
            and (persistent_posture or strong_contact)
            and (very_sudden_drop or (strong_contact and strong_movement))
        )
        if critical_supported:
            return "CRITICAL"
        high_supported = (
            cranial_evidence_count >= 3
            and (strong_movement or sudden_drop or strong_contact)
        ) or (contact and strong_movement)
        if high_supported:
            return "HIGH"
        if base_risk == "LOW" and cranial_evidence_count >= 2 and (moderate_movement or sudden_drop or contact):
            return "MEDIUM"
        return base_risk

    # Core/spinal areas receive higher priority only with severe movement and
    # corroborating impact/posture evidence, not from a region estimate alone.
    if core:
        if (
            general_evidence_count >= 6 and strong_movement and bool(observed_standing)
            and (strong_contact or very_sudden_drop)
            and (persistent_posture or strong_contact)
        ):
            return "CRITICAL"
        severe_support = sum((sudden_drop, horizontal, contact, persistent_posture))
        if strong_movement and severe_support >= 2:
            return "HIGH"
        if base_risk == "LOW" and moderate_movement and (contact or sudden_drop):
            return "MEDIUM"
        return base_risk

    # Knee/leg/shoulder/hip can reach HIGH on strong movement plus evidence of
    # impact or a fall. Minor arm/hand-region motion/contact is capped at MEDIUM
    # unless it is corroborated by both strong contact and a severe fall signal.
    impact = contact or very_sudden_drop or (horizontal and bool(observed_standing) and persistent_posture)
    if (
        general_evidence_count >= 6 and strong_movement and bool(observed_standing)
        and (strong_contact or very_sudden_drop)
        and (persistent_posture or strong_contact)
    ):
        return "CRITICAL"
    if high_priority_limb and strong_movement and impact:
        return "HIGH"
    if low_priority_limb and base_risk == "HIGH" and not (
        strong_contact and (very_sudden_drop or persistent_posture)
    ):
        return "MEDIUM"
    if base_risk == "LOW" and moderate_movement and impact:
        return "MEDIUM"
    return base_risk


# COLLISION DETECTION (IoU between boxes) — only used when ENABLE_COLLISION_ALERTS = True
def calculate_iou(box_a, box_b):
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_width = max(0, inter_x2 - inter_x1)
    inter_height = max(0, inter_y2 - inter_y1)
    inter_area = inter_width * inter_height
    if inter_area == 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union_area = area_a + area_b - inter_area
    if union_area <= 0:
        return 0.0
    return inter_area / union_area


def detect_collisions(player_boxes):
    ids = list(player_boxes.keys())
    current_frame_pairs = set()
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            id_a = ids[a]
            id_b = ids[b]
            iou = calculate_iou(player_boxes[id_a], player_boxes[id_b])
            pair_key = tuple(sorted((id_a, id_b)))
            if iou >= COLLISION_IOU_THRESHOLD:
                current_frame_pairs.add(pair_key)
                collision_streak[pair_key] = collision_streak.get(pair_key, 0) + 1
            else:
                collision_streak[pair_key] = 0
    newly_confirmed = []
    for pair_key, streak in collision_streak.items():
        if (
            streak >= COLLISION_CONFIRM_FRAMES
            and pair_key in current_frame_pairs
            and pair_key not in active_collisions
        ):
            active_collisions.add(pair_key)
            newly_confirmed.append(pair_key)
    for pair_key in list(active_collisions):
        if pair_key not in current_frame_pairs:
            active_collisions.discard(pair_key)
    return newly_confirmed


# DISPLAY HELPERS
# cv2.putText can't draw accented letters (e.g. "Luís Figo"), so strip accents for display.
def display_text(text):
    plain = "".join(
        ch for ch in unicodedata.normalize("NFD", str(text))
        if unicodedata.category(ch) != "Mn"
    )
    return plain.encode("ascii", "ignore").decode()


def jersey_of(player_id):
    if identifier is None or player_id is None:
        return None
    return getattr(identifier, "_number_by_player", {}).get(player_id)


# CLIP RECORDING
# A rolling buffer keeps the last few seconds. When an event fires, those frames
# plus the next few seconds are written to output\clips and attached to the event(s).

pre_buffer = deque(maxlen=max(1, int(CLIP_PRE_SECONDS * EFFECTIVE_FPS)))
POST_FRAMES = max(1, int(CLIP_POST_SECONDS * EFFECTIVE_FPS))
active_clips = []
clip_threads = []


def shrink_for_clip(img):
    h, w = img.shape[:2]
    if w > CLIP_WIDTH:
        scale = CLIP_WIDTH / w
        new_w = CLIP_WIDTH - (CLIP_WIDTH % 2)
        new_h = int(h * scale)
        new_h -= new_h % 2
        return cv2.resize(img, (new_w, new_h))
    return img.copy()


def start_clip(record_id, resource="events"):
    # Records from the same frame share one clip.
    if len(active_clips) > 0 and active_clips[-1]["start_frame"] == frame_count:
        active_clips[-1]["record_refs"].append((resource, record_id))
        return
    active_clips.append({
        "record_refs": [(resource, record_id)],
        "frames": list(pre_buffer),
        "remaining": POST_FRAMES,
        "start_frame": frame_count,
    })


def write_clip(clip):
    frames = clip["frames"]
    if len(frames) == 0:
        return
    refs = [(resource, record_id) for resource, record_id in clip["record_refs"] if record_id is not None]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    first_id = refs[0][1] if refs else "none"
    base = (
        "match" + str(match_id if match_id is not None else 0)
        + "_event" + str(first_id)
        + "_" + stamp
    )
    h, w = frames[0].shape[:2]
    # WebM (VP8) plays directly in the browser; fall back to MP4 if unavailable
    filename = base + ".webm"
    path = os.path.join(CLIPS_DIR, filename)
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"VP80"), EFFECTIVE_FPS, (w, h))
    if not writer.isOpened():
        filename = base + ".mp4"
        path = os.path.join(CLIPS_DIR, filename)
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), EFFECTIVE_FPS, (w, h))
    if not writer.isOpened():
        print("[CLIP] Could not open a video writer — clip not saved")
        return
    for f in frames:
        if f.shape[0] != h or f.shape[1] != w:
            f = cv2.resize(f, (w, h))
        writer.write(f)
    writer.release()
    print("[CLIP] Saved", path)
    for resource, record_id in refs:
        if resource == "collisions":
            attach_collision_clip(record_id, filename)
        else:
            attach_clip(record_id, filename)


def update_clips(small_frame):
    pre_buffer.append(small_frame)
    for clip in list(active_clips):
        clip["frames"].append(small_frame)
        clip["remaining"] -= 1
        if clip["remaining"] <= 0:
            active_clips.remove(clip)
            t = threading.Thread(target=write_clip, args=(clip,))
            t.start()
            clip_threads.append(t)


def flush_clips():
    for clip in list(active_clips):
        active_clips.remove(clip)
        t = threading.Thread(target=write_clip, args=(clip,))
        t.start()
        clip_threads.append(t)
    for t in clip_threads:
        t.join()


# SCENE CUTS
def detect_scene_cut(img):
    global prev_scene_hist
    small = cv2.resize(img, (160, 90))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    is_cut = False
    if prev_scene_hist is not None:
        similarity = cv2.compareHist(prev_scene_hist, hist, cv2.HISTCMP_CORREL)
        is_cut = similarity < SCENE_CUT_THRESHOLD
    prev_scene_hist = hist
    return is_cut


# GAMEPLAY / CLOSE-UP CHECKS
def pitch_green_fraction(img):
    """Share of the frame that looks like grass (green, saturated, not dark)."""
    small = cv2.resize(img, (160, 90))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (35, 60, 40), (85, 255, 255))
    return float(cv2.countNonZero(mask)) / mask.size


def legs_visible(conf):
    """True if at least one knee or ankle is detected with confidence."""
    return any(
        float(conf[k]) >= KEYPOINT_CONFIDENCE
        for k in (LEFT_KNEE, RIGHT_KNEE, LEFT_ANKLE, RIGHT_ANKLE)
    )


def cut_between(frame_a, frame_b):
    """True if a camera cut happened after frame_a and at or before frame_b."""
    return any(frame_a < f <= frame_b for f in scene_cut_frames)


# TRACK LINEAGE
# When a player falls, the tracker often gives them a NEW track number.
# The old track (which may already know who they are) vanishes at the same spot.
def box_center_size(box):
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0), max(x2 - x1, y2 - y1, 1.0)


def find_parent_tracks(track_id):
    """Tracks that disappeared right where this track first appeared, closest first."""
    first = track_first_seen.get(track_id)
    if first is None:
        return []
    f0, box0 = first
    (cx0, cy0), size0 = box_center_size(box0)
    max_gap = LINEAGE_MAX_GAP_SECONDS * EFFECTIVE_FPS
    candidates = []
    for other, (fl, boxl) in track_last_seen.items():
        if other == track_id:
            continue
        other_first = track_first_seen.get(other)
        if other_first is None or other_first[0] >= f0:
            continue                       # must be an older track
        if fl > f0 + 3 or fl < f0 - max_gap:
            continue                       # must have vanished around when this one appeared
        if fl >= frame_count:
            continue                       # still visible -> it's someone else
        if cut_between(fl, f0):
            continue                       # never link across a camera cut
        (cxl, cyl), sizel = box_center_size(boxl)
        distance = math.hypot(cx0 - cxl, cy0 - cyl) / max(size0, sizel)
        if distance <= LINEAGE_MAX_DISTANCE:
            candidates.append((distance, other))
    candidates.sort()
    return [other for _, other in candidates]


def find_child_tracks(track_id):
    """Tracks that appeared right where this track disappeared (the same player, renumbered)."""
    last = track_last_seen.get(track_id)
    if last is None:
        return []
    fl, boxl = last
    if fl >= frame_count:
        return []                          # still visible, no successor yet
    (cxl, cyl), sizel = box_center_size(boxl)
    max_gap = LINEAGE_MAX_GAP_SECONDS * EFFECTIVE_FPS
    candidates = []
    for other, (f0, box0) in track_first_seen.items():
        if other == track_id:
            continue
        if f0 < fl - 3 or f0 > fl + max_gap:
            continue                       # must have appeared around when this one vanished
        if cut_between(fl, f0):
            continue
        (cx0, cy0), size0 = box_center_size(box0)
        distance = math.hypot(cx0 - cxl, cy0 - cyl) / max(size0, sizel)
        if distance <= LINEAGE_MAX_DISTANCE:
            candidates.append((distance, other))
    candidates.sort()
    return [other for _, other in candidates]


def follow_forward_identity(track_ids):
    """Identity of the first identified successor of these tracks (breadth-first), or None."""
    seen = set(track_ids)
    frontier = list(track_ids)
    for depth in range(FOLLOW_FORWARD_MAX_DEPTH):
        next_frontier = []
        for tid in frontier:
            for child in find_child_tracks(tid)[:2]:
                if child in seen:
                    continue
                seen.add(child)
                ident = track_identity.get(child)
                if ident is not None and ident.get("player_id") is not None:
                    return child, ident
                next_frontier.append(child)
        if not next_frontier:
            break
        frontier = next_frontier
    return None, None


def identity_of_track(track_id, depth=0):
    """Known identity of a track: confirmed, read from its saved crops, or inherited."""
    ident = track_identity.get(track_id)
    if ident is not None:
        return ident
    if identifier is None:
        return None
    try:
        found = identifier.identify_from_history(track_id)
    except Exception:
        found = None
    if found is not None and found.get("player_id") is not None:
        track_identity[track_id] = found
        return found
    if depth < 2:
        for parent in find_parent_tracks(track_id)[:3]:
            parent_ident = identity_of_track(parent, depth + 1)
            if parent_ident is not None:
                return parent_ident
    return None


def resolve_fall_identity(track_id, frame, box):
    """Best identity for the player who just fell, or None."""
    global stats_inherited
    ident = track_identity.get(track_id)
    if ident is not None:
        return ident
    if identifier is None:
        return None
    # 1) this track's own jersey crops from before the fall
    try:
        found = identifier.identify_from_history(track_id)
    except Exception as e:
        found = None
        print("[ID] History lookup failed on track", track_id, ":", e)
    if found is not None and found.get("player_id") is not None:
        track_identity[track_id] = found
        print("[ID] Track", track_id, "->", found["player_name"], "via", found["method"], "(before saving event)")
        return found
    # 2) the track that vanished where this one appeared (tracker renumbered the player)
    shirt_team = identifier._guess_team_id(frame, box)
    for parent in find_parent_tracks(track_id)[:3]:
        parent_ident = identity_of_track(parent)
        if parent_ident is None or parent_ident.get("player_id") is None:
            continue
        player_team = identifier._team_by_player.get(parent_ident["player_id"])
        if shirt_team is not None and player_team is not None and shirt_team != player_team:
            continue                       # shirt colour says it's the other team
        inherited = {
            "player_id": parent_ident["player_id"],
            "player_name": parent_ident["player_name"],
            "method": "jersey_handoff",
            "detail": "carried from track " + str(parent) + " (" + str(parent_ident.get("detail")) + ")",
            "confidence": round(float(parent_ident.get("confidence") or 0) * 0.9, 3),
            "vote_count": parent_ident.get("vote_count", 1),
        }
        track_identity[track_id] = inherited
        stats_inherited += 1
        print("[ID] Track", track_id, "->", inherited["player_name"], "inherited from track", parent)
        return inherited
    return None


# INCIDENTS — one fall = one event
def find_incident(box):
    (cx, cy), size = box_center_size(box)
    window = INCIDENT_MERGE_SECONDS * EFFECTIVE_FPS
    best = None
    for inc in recent_incidents:
        if frame_count - inc["frame"] > window:
            continue
        if cut_between(inc["frame"], frame_count):
            continue
        (ix, iy), isize = box_center_size(inc["box"])
        distance = math.hypot(cx - ix, cy - iy) / max(size, isize)
        if distance <= INCIDENT_MERGE_DISTANCE and (best is None or distance < best[0]):
            best = (distance, inc)
    return best[1] if best else None


# EVENT RECORDING
# Saved immediately. If the player isn't known yet, it's saved as Unidentified
# and queued so the name is filled in on the same event later.
def fall_cooldown_over(track_id):
    last = last_fall_frame.get(track_id)
    return last is None or frame_count - last >= FALL_COOLDOWN_SECONDS * EFFECTIVE_FPS


def record_event(track_id, event_type, region, movement, risk, log_name, frame=None, box=None):
    global stats_merged_falls
    ident = None
    if event_type == "FALL" and frame is not None and box is not None:
        ident = resolve_fall_identity(track_id, frame, box)
    else:
        ident = track_identity.get(track_id)
    # ---- same incident as a fall saved a moment ago? merge instead of a new event ----
    if event_type == "FALL" and box is not None:
        incident = find_incident(box)
        if incident is not None:
            stats_merged_falls += 1
            incident["tracks"].append(track_id)
            print("[MERGE] Fall on track", track_id, "is the same incident as event", incident["event_id"], "- not saved again")
            if not incident["identified"] and ident is not None and incident["event_id"] is not None:
                update_event_player(
                    incident["event_id"],
                    ident["player_id"],
                    ident["player_name"],
                    jersey_number=jersey_of(ident["player_id"]),
                    event_type=event_type,
                    match_name=MATCH_NAME,
                    identified_by=ident["method"],
                    id_detail=ident["detail"],
                    id_confidence=ident["confidence"],
                )
                incident["identified"] = True
                pending_identification.pop(incident["event_id"], None)
                for e in event_log:
                    if e["event_id"] == incident["event_id"]:
                        e["player"] = ident["player_name"]
                        e["identified_by"] = ident["method"]
            elif not incident["identified"] and incident["event_id"] in pending_identification:
                pending_identification[incident["event_id"]]["tracks"].append(track_id)
            return
    player_id = ident["player_id"] if ident else None
    player_name = ident["player_name"] if ident else None
    video_time_sec = round(frame_count / EFFECTIVE_FPS, 2)
    event_id = save_event(
        event_type=event_type,
        region=region,
        movement=movement,
        risk=risk,
        track_id=track_id,
        player_id=player_id,
        player_name=player_name,
        jersey_number=jersey_of(player_id),
        match_id=match_id,
        match_name=MATCH_NAME,
        identified_by=ident["method"] if ident else None,
        id_detail=ident["detail"] if ident else None,
        id_confidence=ident["confidence"] if ident else None,
        video_time_sec=video_time_sec,
    )
    if event_id is not None and player_id is None and identifier is not None:
        pending_identification[event_id] = {
            "tracks": [track_id],
            "frame": frame_count,
            "event_type": event_type,
        }
    if event_type == "FALL" and box is not None:
        recent_incidents.append({
            "event_id": event_id,
            "frame": frame_count,
            "box": box,
            "tracks": [track_id],
            "identified": player_id is not None,
        })
        # keep only recent incidents
        cutoff = frame_count - 10 * EFFECTIVE_FPS
        recent_incidents[:] = [i for i in recent_incidents if i["frame"] >= cutoff]
    start_clip(event_id)
    event_log.append({
        "frame": frame_count,
        "time_sec": round(frame_count / EFFECTIVE_FPS, 2),
        "event_id": event_id,
        "track_id": track_id,
        "player": player_name or "Unidentified",
        "identified_by": ident["method"] if ident else "-",
        "event": log_name,
        "region": region,
        "movement": round(float(movement or 0), 2),
        "risk": risk,
    })


def record_collision(pair_key, frame, boxes, overlap):
    track_ids = tuple(sorted(pair_key))
    participants = []
    for slot, track_id in enumerate(track_ids, start=1):
        identity = track_identity.get(track_id)
        participants.append((
            slot,
            track_id,
            identity,
            {
                "region": player_region.get(track_id),
                "movement": player_movement.get(track_id),
            },
        ))

    height, width = frame.shape[:2]
    x1 = min(box[0] for box in boxes)
    y1 = min(box[1] for box in boxes)
    x2 = max(box[2] for box in boxes)
    y2 = max(box[3] for box in boxes)
    video_time_sec = round(frame_count / EFFECTIVE_FPS, 2)
    collision_id = save_collision(
        match_id=match_id,
        track_ids=track_ids,
        participants=participants,
        video_time_sec=video_time_sec,
        frame_number=frame_count,
        risk="MEDIUM",
        iou=overlap,
        contact_frames=collision_streak.get(track_ids),
        location_x=min(1.0, max(0.0, (x1 + x2) / (2 * width))),
        location_y=min(1.0, max(0.0, (y1 + y2) / (2 * height))),
    )
    start_clip(collision_id, resource="collisions")
    event_log.append({
        "frame": frame_count,
        "time_sec": video_time_sec,
        "event_id": collision_id,
        "track_id": track_ids[0],
        "track_ids": track_ids,
        "player": " / ".join(
            track_identity[track_id]["player_name"]
            for track_id in track_ids
            if track_id in track_identity
        ) or "Unidentified",
        "identified_by": " / ".join(
            track_identity[track_id]["method"]
            for track_id in track_ids
            if track_id in track_identity
        ) or "-",
        "event": "COLLISION",
        "region": " / ".join(
            str(player_region[track_id])
            for track_id in track_ids
            if player_region.get(track_id)
        ) or "UNKNOWN",
        "movement": round(sum(float(player_movement.get(track_id) or 0) for track_id in track_ids), 2),
        "risk": "MEDIUM",
    })


def resolve_pending_identifications():
    global stats_followed_forward
    window = IDENTIFY_WINDOW_SECONDS * EFFECTIVE_FPS
    for event_id, info in list(pending_identification.items()):
        ident = None
        for tid in info["tracks"]:
            ident = track_identity.get(tid)
            if ident is not None:
                break
        # the player got up under a new track number and was identified there
        if ident is None and frame_count % FOLLOW_FORWARD_EVERY_N_FRAMES == 0:
            child, child_ident = follow_forward_identity(info["tracks"])
            if child_ident is not None:
                stats_followed_forward += 1
                ident = {
                    "player_id": child_ident["player_id"],
                    "player_name": child_ident["player_name"],
                    "method": "jersey_handoff",
                    "detail": "identified later on track " + str(child) + " (" + str(child_ident.get("detail")) + ")",
                    "confidence": round(float(child_ident.get("confidence") or 0) * 0.9, 3),
                    "vote_count": child_ident.get("vote_count", 1),
                }
                print("[ID] Event", event_id, "-> " + ident["player_name"] + " (followed the player to track", child, ")")
        if ident is not None:
            update_event_player(
                event_id,
                ident["player_id"],
                ident["player_name"],
                jersey_number=jersey_of(ident["player_id"]),
                event_type=info["event_type"],
                match_name=MATCH_NAME,
                identified_by=ident["method"],
                id_detail=ident["detail"],
                id_confidence=ident["confidence"],
            )
            for e in event_log:
                if e["event_id"] == event_id:
                    e["player"] = ident["player_name"]
                    e["identified_by"] = ident["method"]
            for inc in recent_incidents:
                if inc["event_id"] == event_id:
                    inc["identified"] = True
            del pending_identification[event_id]
        elif frame_count - info["frame"] > window:
            print("[ID] Event", event_id, "stays Unidentified (no ID within", IDENTIFY_WINDOW_SECONDS, "s)")
            del pending_identification[event_id]


# VIDEO LOOP

gui_available = True

while True:
    ret, frame = cap.read()
    if not ret:
        print("End of video reached (or read failed)")
        break
    frame_count += 1
    if detect_scene_cut(frame):
        scene_cut_frames.append(frame_count)
        scene_cut_frames[:] = scene_cut_frames[-200:]
        if DEBUG_FALL_METRICS:
            print("[SCENE] camera cut at frame", frame_count)
    frame_h = frame.shape[0]
    green_fraction = pitch_green_fraction(frame)
    is_gameplay_shot = green_fraction >= MIN_PITCH_GREEN_FRACTION
    if not is_gameplay_shot:
        stats_non_gameplay_frames += 1
    results = model.track(
        frame,
        persist=True,
        verbose=False,
        device=DEVICE,
        imgsz=INFER_IMGSZ,
        tracker=TRACKER_CONFIG,
    )
    result = results[0]
    annotated_frame = result.plot()
    player_boxes_this_frame = {}
    if (
        result.boxes is not None
        and result.boxes.id is not None
        and result.keypoints is not None
    ):
        ids = result.boxes.id.int().cpu().tolist()
        all_boxes = result.boxes.xyxy.cpu().numpy()
        # every detected track, valid or not (needed to link renumbered players)
        for j, tid in enumerate(ids):
            b = tuple(float(v) for v in all_boxes[j])
            track_first_seen.setdefault(int(tid), (frame_count, b))
            track_last_seen[int(tid)] = (frame_count, b)
        keypoints = result.keypoints.xy.cpu().numpy()
        if result.keypoints.conf is not None:
            confidences = result.keypoints.conf.cpu().numpy()
        else:
            confidences = None
        any_valid_player_this_frame = False
        for i, track_id in enumerate(ids):
            track_id = int(track_id)
            points = keypoints[i]
            if confidences is not None:
                conf = confidences[i]
            else:
                conf = [1.0] * 17
            box = result.boxes[i]
            x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].cpu().numpy()]
            box_height = float(y2 - y1)
            box_width = float(x2 - x1)
            box_confidence = float(box.conf[0])
            strict_ok = is_strict_valid(box_confidence, points, conf, box_height)
            if strict_ok:
                valid_streak[track_id] = valid_streak.get(track_id, 0) + 1
                if valid_streak[track_id] >= CONFIRM_FRAMES:
                    confirmed_players.add(track_id)
            else:
                valid_streak[track_id] = 0
            is_confirmed = track_id in confirmed_players
            if not is_confirmed:
                if not strict_ok:
                    if DEBUG_FALL_METRICS:
                        print(
                            "[DEBUG] track", track_id,
                            "REJECTED (not confirmed) box_conf=", round(box_confidence, 2),
                            "box_h=", round(box_height, 1),
                            "streak=", valid_streak.get(track_id, 0)
                        )
                    continue
            else:
                if box_confidence < CONTINUE_CONFIDENCE:
                    if DEBUG_FALL_METRICS:
                        print(
                            "[DEBUG] track", track_id,
                            "DROPPED this frame: box_conf", round(box_confidence, 2),
                            "< CONTINUE_CONFIDENCE"
                        )
                    continue
                if (
                    float(conf[LEFT_SHOULDER]) < KEYPOINT_CONFIDENCE
                    or float(conf[RIGHT_SHOULDER]) < KEYPOINT_CONFIDENCE
                ):
                    if DEBUG_FALL_METRICS:
                        print("[DEBUG] track", track_id, "DROPPED this frame: shoulders not visible")
                    continue
            any_valid_player_this_frame = True
            player_boxes_this_frame[track_id] = (float(x1), float(y1), float(x2), float(y2))

            # PLAYER IDENTIFICATION (every frame — the identifier returns its cached answer
            # once a track is confirmed, and needs every frame to keep hand-off tracking accurate)
            if identifier is not None:
                try:
                    ident = identifier.identify(
                        track_id, frame,
                        (float(x1), float(y1), float(x2), float(y2)),
                        frame_index=frame_count,
                        keypoints=points,
                        keypoint_conf=conf,
                    )
                except Exception as e:
                    ident = None
                    if DEBUG_FALL_METRICS:
                        print("[ID] identify() error on track", track_id, ":", e)
                # keep recent jersey crops so a fall can be identified
                # from the frames BEFORE the player went down
                try:
                    identifier.remember_crop(
                        track_id, frame,
                        (float(x1), float(y1), float(x2), float(y2)),
                        frame_count,
                        keypoints=points,
                        keypoint_conf=conf,
                    )
                except Exception as e:
                    if DEBUG_FALL_METRICS:
                        print("[ID] remember_crop() error on track", track_id, ":", e)
                if (
                    ident is not None
                    and ident.get("player_id") is not None
                    and track_id not in track_identity
                ):
                    track_identity[track_id] = ident
                    print("[ID] Track", track_id, "->", ident["player_name"], "via", ident["method"])
                # face recognition switches on by itself once the first face is learned
                if not face_announced and identifier.face_ready:
                    face_announced = True
                    print("[ID] Face recognition: ON (faces learned automatically from jersey reads)")

            # FALLS ONLY DURING PLAY: not on close-ups of coaches/crowd, and only when legs are visible
            is_closeup = box_height > MAX_BOX_HEIGHT_FRACTION * frame_h
            fall_checks_allowed = (
                is_gameplay_shot
                and not is_closeup
                and (legs_visible(conf) or not REQUIRE_LEGS_FOR_FALL)
            )

            # STATIC POSTURE CHECK
            is_horizontal, torso_angle = compute_static_posture(points, conf, box_width, box_height)
            if not fall_checks_allowed:
                is_horizontal = False
            if is_horizontal:
                posture_streak[track_id] = posture_streak.get(track_id, 0) + 1
            else:
                posture_streak[track_id] = 0
                if torso_angle is not None and torso_angle < 35:
                    ever_upright[track_id] = True
            was_on_ground = ground_state.get(track_id, False)
            if (
                posture_streak[track_id] >= POSTURE_CONFIRM_FRAMES
                and not was_on_ground
                and not ever_upright.get(track_id, False)
                and not any(ever_upright.get(p, False) for p in find_parent_tracks(track_id))
            ):
                # appeared already lying down, and nobody was standing there before:
                # a camera cut / replay / partly visible player, not a fall we saw happen
                ground_state[track_id] = True
                stats_skipped_no_upright += 1
                if DEBUG_FALL_METRICS:
                    print("[SKIP] Track", track_id, "appeared already lying down (camera cut / replay) - not a fall")
            elif posture_streak[track_id] >= POSTURE_CONFIRM_FRAMES and not was_on_ground:
                ground_state[track_id] = True
                player_status[track_id] = "FALL"
                if track_id in player_history and len(player_history[track_id]) >= 2:
                    region, movement = calculate_region_movement(player_history[track_id])
                    base_risk = calculate_risk(movement)
                else:
                    region, movement = estimate_posture_region(points, conf, float(y1), box_height)
                    aspect_ratio = box_width / box_height if box_height > 0 else 0
                    base_risk = calculate_posture_risk(torso_angle, aspect_ratio)
                aspect_ratio = box_width / box_height if box_height > 0 else 0
                risk = calculate_injury_risk(
                    base_risk, region, movement,
                    _current_downward_ratio(player_history.get(track_id, []), points, conf, box_height),
                    torso_angle, aspect_ratio,
                    _head_to_lower_anchor_gap(points, conf, box_height),
                    posture_streak[track_id], ever_upright.get(track_id, False),
                    active_contact_frames(track_id),
                )
                player_region[track_id] = region
                player_movement[track_id] = movement
                player_risk[track_id] = risk
                print()
                print("====================================")
                print("🚨 FALL DETECTED (POSTURE — PLAYER ON GROUND)")
                print("====================================")
                print("TRACK:", track_id)
                print("TORSO ANGLE:", round(torso_angle, 2) if torso_angle is not None else "N/A")
                print("REGION:", region)
                print("RISK:", risk)
                print("====================================")
                if fall_cooldown_over(track_id):
                    last_fall_frame[track_id] = frame_count
                    record_event(
                        track_id, "FALL", region, movement, risk, "FALL_POSTURE",
                        frame=frame, box=(float(x1), float(y1), float(x2), float(y2)),
                    )
                else:
                    print("[INFO] Fall on track", track_id, "within cooldown — not saved again")
            elif posture_streak[track_id] == 0:
                ground_state[track_id] = False

            # NECK + LOWER ANCHOR FOR THIS FRAME
            neck, neck_conf = get_neck(points, conf)
            lower_anchor = get_lower_anchor(points, conf)
            if lower_anchor is None and is_confirmed:
                if (
                    track_id in player_history
                    and len(player_history[track_id]) > 0
                    and player_history[track_id][-1]["lower_anchor"] is not None
                ):
                    lower_anchor = player_history[track_id][-1]["lower_anchor"]
            if track_id not in player_history:
                player_history[track_id] = []
                player_status[track_id] = player_status.get(track_id, "NORMAL")
                player_region[track_id] = player_region.get(track_id, "NONE")
                player_risk[track_id] = player_risk.get(track_id, "LOW")
                player_movement[track_id] = player_movement.get(track_id, 0)
            player_history[track_id].append({
                "neck": neck,
                "lower_anchor": lower_anchor,
                "points": points,
                "conf": conf,
                "box_height": box_height
            })
            if len(player_history[track_id]) > 10:
                player_history[track_id].pop(0)
            fall_detected = fall_checks_allowed and detect_fall(player_history[track_id])
            dr, ang = compute_fall_metrics(player_history[track_id])
            if DEBUG_FALL_METRICS and dr is not None:
                print(
                    "[DEBUG] track", track_id,
                    "confirmed=", is_confirmed,
                    "box_conf=", round(box_confidence, 2),
                    "box_h=", round(box_height, 1),
                    "downward_ratio=", round(dr, 3),
                    "angle=", round(ang, 2),
                    "posture_horizontal=", is_horizontal,
                    "status=", player_status[track_id]
                )

            # CALIBRATION LOGGING
            aspect_ratio_log = box_width / box_height if box_height > 0 else 0
            calibration_writer.writerow([
                frame_count,
                round(frame_count / EFFECTIVE_FPS, 2),
                track_id,
                round(dr, 4) if dr is not None else "",
                round(ang, 2) if ang is not None else "",
                round(torso_angle, 2) if torso_angle is not None else "",
                round(aspect_ratio_log, 3),
                is_horizontal,
                fall_detected,
                posture_streak[track_id] >= POSTURE_CONFIRM_FRAMES,
                ""
            ])

            # MOVEMENT-BASED FALL
            if fall_detected and not ground_state.get(track_id, False):
                ground_state[track_id] = True
                player_status[track_id] = "FALL"
                region, movement = calculate_region_movement(player_history[track_id])
                if region == "UNKNOWN":
                    region, movement = estimate_posture_region(points, conf, float(y1), box_height)
                player_region[track_id] = region
                player_movement[track_id] = movement
                risk = calculate_injury_risk(
                    calculate_risk(movement), region, movement, dr, torso_angle,
                    aspect_ratio_log, _head_to_lower_anchor_gap(points, conf, box_height),
                    posture_streak[track_id], ever_upright.get(track_id, False),
                    active_contact_frames(track_id),
                )
                player_risk[track_id] = risk
                print()
                print("====================================")
                print("🚨 FALL DETECTED (MOVEMENT)")
                print("====================================")
                print("TRACK:", track_id)
                print("REGION:", region)
                print("MOVEMENT:", round(movement, 2))
                print("RISK:", risk)
                print("====================================")
                if fall_cooldown_over(track_id):
                    last_fall_frame[track_id] = frame_count
                    record_event(
                        track_id, "FALL", region, movement, risk, "FALL_MOVEMENT",
                        frame=frame, box=(float(x1), float(y1), float(x2), float(y2)),
                    )
                else:
                    print("[INFO] Fall on track", track_id, "within cooldown — not saved again")

            # ON-SCREEN LABELS
            status = player_status[track_id]
            region = player_region[track_id]
            risk = player_risk[track_id]
            movement = player_movement[track_id]
            ident = track_identity.get(track_id)
            if ident is not None:
                label = display_text(ident["player_name"])
                number = jersey_of(ident["player_id"])
                if number is not None:
                    label += " #" + str(number)
                label += " [FACE]" if ident["method"] == "face" else " [JERSEY]"
            else:
                label = "PLAYER T" + str(track_id)
            text_x = x1
            text_y = max(y1 - 100, 30)
            cv2.putText(
                annotated_frame, label,
                (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2
            )
            if status == "FALL" and fall_checks_allowed:
                cv2.putText(
                    annotated_frame, "REGION: " + region,
                    (text_x, text_y + 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 165, 255), 2
                )
                cv2.putText(
                    annotated_frame, "RISK: " + risk,
                    (text_x, text_y + 75), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2
                )
                cv2.putText(
                    annotated_frame, "MOVEMENT: " + str(round(movement, 2)),
                    (text_x, text_y + 100), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2
                )

        # COLLISIONS — skipped unless ENABLE_COLLISION_ALERTS = True
        if ENABLE_COLLISION_ALERTS:
            newly_confirmed_collisions = detect_collisions(player_boxes_this_frame)
            for pair_key in newly_confirmed_collisions:
                id_a, id_b = pair_key
                player_status[id_a] = "COLLISION"
                player_status[id_b] = "COLLISION"
                print()
                print("====================================")
                print("⚠️ COLLISION DETECTED")
                print("====================================")
                print("TRACKS:", id_a, "and", id_b)
                print("====================================")
                last = last_collision_frame.get(pair_key)
                if last is None or frame_count - last >= COLLISION_COOLDOWN_SECONDS * EFFECTIVE_FPS:
                    last_collision_frame[pair_key] = frame_count
                    record_collision(
                        pair_key,
                        frame,
                        [player_boxes_this_frame[tid] for tid in pair_key],
                        calculate_iou(
                            player_boxes_this_frame[id_a],
                            player_boxes_this_frame[id_b],
                        ),
                    )
        if not any_valid_player_this_frame:
            cv2.putText(
                annotated_frame, "NO VALID PLAYER",
                (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2
            )
    else:
        cv2.putText(
            annotated_frame, "NO PLAYER DETECTED",
            (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2
        )

    # FILL IN PLAYERS ON UNIDENTIFIED EVENTS
    resolve_pending_identifications()
    banner_y = annotated_frame.shape[0] - 20
    cv2.putText(
        annotated_frame, "AI INJURY DETECTION v2 | " + display_text(MATCH_NAME),
        (20, banner_y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2
    )
    if not is_gameplay_shot:
        cv2.putText(
            annotated_frame, "NOT GAMEPLAY (close-up / crowd) - fall detection paused",
            (20, banner_y - 35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2
        )
    # CLIP BUFFER (after all drawing, so clips include the labels and alerts)
    update_clips(shrink_for_clip(annotated_frame))
    if gui_available:
        try:
            cv2.imshow("AI Injury Detection v2", annotated_frame)
            key = cv2.waitKey(max(1, int(1000 / EFFECTIVE_FPS))) & 0xFF
        except cv2.error as exc:
            gui_available = False
            print("[DISPLAY] OpenCV GUI unavailable; continuing without preview:", exc)
        else:
            if key == ord("q"):
                print("Q pressed")
                break

# SHUTDOWN

cap.release()
if gui_available:
    try:
        cv2.destroyAllWindows()
    except cv2.error as exc:
        print("[DISPLAY] Could not close OpenCV windows:", exc)
calibration_log.close()

print()
print("[CLIP] Finishing clips...")
flush_clips()

for event_id in list(pending_identification.keys()):
    print("[ID] Event", event_id, "stays Unidentified (video ended)")

if identifier is not None:
    identifier.print_report()

finish_notifications()

print()
print("====================================")
print(" DETECTION STOPPED")
print("====================================")

falls_saved = [e for e in event_log if e["event"].startswith("FALL")]
falls_identified = [e for e in falls_saved if e["player"] != "Unidentified"]
id_rate = (100.0 * len(falls_identified) / len(falls_saved)) if falls_saved else 0.0

print()
print("====================================")
print(" FALL IDENTIFICATION")
print("====================================")
print(" Falls saved:                 ", len(falls_saved))
print(" Identified:                  ", len(falls_identified), "(" + str(round(id_rate)) + "%)")
print(" Merged into an earlier event:", stats_merged_falls, "(same incident, tracker renumbered the player)")
print(" Identity carried from the vanished track:", stats_inherited)
print(" Identity found later (player followed after getting up):", stats_followed_forward)
print(" Ignored (appeared already lying down):", stats_skipped_no_upright)
print(" Camera cuts detected:        ", len(scene_cut_frames), "(last 200 kept)")
print(" Non-gameplay frames skipped: ", stats_non_gameplay_frames, "(close-ups, crowd, benches)")
print("====================================")

print()
print("====================================")
print(" FINAL SUMMARY —", len(event_log), "EVENT(S) SAVED | MATCH:", MATCH_NAME)
print("====================================")

for e in event_log:
    print(
        "[t=" + str(e["time_sec"]) + "s]",
        "event", e["event_id"], "|",
        "track", e["track_id"], "|",
        e["player"], "(" + str(e["identified_by"]) + ")", "|",
        e["event"], "|",
        "region:", e["region"], "|",
        "risk:", e["risk"]
    )

print("====================================")
