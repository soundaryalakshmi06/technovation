"""Small in-process worker for uploaded AthleteGuard videos.

This module is intentionally import-safe: importing it does not load weights,
open video files, or connect to external notification services. A request marks
the Video/Match PROCESSING, then submits the work to a single background thread.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger("athleteguard.video_processing")
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="athleteguard-video")
_lock = threading.Lock()
_active: Set[int] = set()


def is_processing(video_id: int) -> bool:
    with _lock:
        return video_id in _active


def enqueue_video(video_id: int) -> bool:
    """Submit one video, returning False if that video is already queued/running."""
    with _lock:
        if video_id in _active:
            return False
        _active.add(video_id)
    try:
        _executor.submit(_process_video, video_id)
    except Exception:
        with _lock:
            _active.discard(video_id)
        raise
    return True


def _iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    union += max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0.0


def _classify_fall_risk(base_risk_score: float, region: Optional[str], confidence: float,
                        seen_standing: bool, horizontal_streak: int,
                        head_motion: Optional[float]) -> Tuple[str, float]:
    """Escalate only corroborated cranial-impact signals; a region label alone is insufficient."""
    risk_score = min(1.0, max(0.0, base_risk_score))
    cranial_region = str(region or "").strip().upper() in {"HEAD", "FACE", "EYE", "EYES", "NECK"}
    critical_supported = (
        cranial_region
        and seen_standing
        and horizontal_streak >= 2
        and confidence >= 0.8
        and head_motion is not None
        and head_motion >= 0.45
    )
    if critical_supported:
        return "CRITICAL", max(risk_score, 0.9)
    risk = "HIGH" if risk_score >= 0.75 else "MEDIUM" if risk_score >= 0.5 else "LOW"
    return risk, risk_score


def _classify_collision_risk(overlap: float, contact_frames: int,
                             associated_injury_events) -> Tuple[str, float]:
    """Keep geometric contact low risk unless an associated fall classifier corroborates it."""
    contact_score = min(0.49, max(
        0.0,
        0.20 + 0.20 * min(1.0, max(0.0, overlap))
        + 0.015 * min(max(0, contact_frames), 8),
    ))
    severity_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    corroborating = [
        event for event in associated_injury_events
        if event.risk in severity_rank
    ]
    if not corroborating:
        return "LOW", contact_score
    strongest = max(corroborating, key=lambda event: severity_rank[event.risk])
    return strongest.risk, max(contact_score, float(strongest.risk_score or 0.0))


def _model_path() -> Path:
    configured = os.environ.get("ATHLETEGUARD_POSE_MODEL")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / "yolo11n-pose.pt").resolve()


def _identity_payload(identifier, track_id: int, frame, box, frame_number: int,
                      keypoints, keypoint_conf, allowed_teams):
    """Return a roster identity only when OCR evidence clears the configured threshold."""
    if identifier is None:
        return None
    try:
        result = identifier.identify(
            track_id, frame, box, frame_index=frame_number,
            keypoints=keypoints, keypoint_conf=keypoint_conf,
        )
    except Exception as exc:
        logger.warning("identity lookup skipped for track %s: %s", track_id, type(exc).__name__)
        return None
    if not result or result.get("player_id") is None:
        return None
    confidence = result.get("confidence")
    from config import settings
    if confidence is None or float(confidence) < settings.IDENTITY_CONFIRMED_MIN_CONFIDENCE:
        return None
    try:
        player_id = int(result["player_id"])
        player_team_id = identifier._team_by_player.get(player_id)
    except Exception:
        return None
    if player_team_id not in allowed_teams:
        return None
    payload = {
        "player_id": player_id,
        "team_id": player_team_id,
        "confidence": min(1.0, max(0.0, float(confidence))),
        "method": result.get("method") if result.get("method") in {
            "jersey", "jersey_name", "jersey_history", "jersey_handoff", "face"
        } else "jersey",
        "detail": str(result.get("detail") or "roster jersey match")[:500],
    }
    if result.get("method") == "jersey_handoff" and result.get("from_track") is not None:
        payload["predecessor_track_id"] = int(result["from_track"])
    return payload


def _preceding_collision_id(db, CollisionEvent, match_id: int, video_id: int,
                            player_id, track_id: int, video_time_sec: float,
                            window_seconds: float) -> Optional[int]:
    """Link only a recent collision involving this same player or track in this video."""
    candidates = (
        db.query(CollisionEvent)
        .filter(
            CollisionEvent.match_session_id == match_id,
            CollisionEvent.video_id == video_id,
            CollisionEvent.video_time_sec <= video_time_sec,
            CollisionEvent.video_time_sec >= video_time_sec - window_seconds,
        )
        .order_by(CollisionEvent.video_time_sec.desc(), CollisionEvent.id.desc())
        .all()
    )
    for collision in candidates:
        for participant in collision.participants:
            if player_id is not None and participant.player_id == player_id:
                return collision.id
            same_track = participant.track_id == track_id
            identity_not_conflicting = (
                participant.player_id is None
                or player_id is None
                or participant.player_id == player_id
            )
            if same_track and identity_not_conflicting:
                return collision.id
    return None


def _process_video(video_id: int) -> None:
    from config import settings
    from database import SessionLocal
    from models import (
        Alert, CollisionEvent, CollisionParticipant, InjuryEvent, MatchSession,
        Player, PlayerTrack, Video, build_collision_dedupe_key,
        build_fall_dedupe_key,
    )

    db = SessionLocal()
    cap = None
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if video is None:
            raise RuntimeError("Video record no longer exists")
        match = db.query(MatchSession).filter(MatchSession.id == video.match_session_id).first()
        if match is None:
            raise RuntimeError("The video's match record no longer exists")
        source = Path(video.file_path or "").expanduser().resolve()
        if not source.is_file():
            raise RuntimeError("Uploaded video file is missing from server storage")
        video_root = Path(settings.VIDEOS_DIR).expanduser().resolve()
        try:
            source.relative_to(video_root)
        except ValueError:
            raise RuntimeError("Video file path is outside the configured upload directory")
        weights = _model_path()
        if not weights.is_file():
            raise RuntimeError(
                "YOLO11-Pose weights are missing. Set ATHLETEGUARD_POSE_MODEL to an existing yolo11*-pose.pt file."
            )

        import cv2
        import numpy as np
        from ultralytics import YOLO

        cap = cv2.VideoCapture(str(source))
        if not cap.isOpened():
            raise RuntimeError("OpenCV could not open the uploaded video")
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if fps <= 0:
            fps = 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        declared_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if width <= 0 or height <= 0:
            raise RuntimeError("The uploaded video has invalid frame dimensions")

        model = YOLO(str(weights))
        pose_shape = getattr(getattr(model, "model", None), "kpt_shape", None)
        names = getattr(model, "names", {})
        if pose_shape is not None and int(pose_shape[-2]) != 17:
            raise RuntimeError("Configured YOLO weights do not expose the expected 17 COCO pose keypoints")
        if not isinstance(names, dict) or not any(str(name).lower() == "person" for name in names.values()):
            raise RuntimeError("Configured weights are not a person pose model")
        tracker_cfg = Path(__file__).with_name("tracker_botsort_reid.yaml")
        if not tracker_cfg.is_file():
            raise RuntimeError("The configured BoT-SORT tracker file is missing")

        allowed_teams = {team_id for team_id in (match.team_a_id, match.team_b_id) if team_id is not None}
        identifier = None
        try:
            ocr_cache = Path.home() / ".EasyOCR" / "model"
            if all((ocr_cache / filename).is_file() for filename in ("craft_mlt_25k.pth", "english_g2.pth")):
                from player_identifier import PlayerIdentifier
                identifier = PlayerIdentifier(
                    enable_face=False, auto_face_gallery=False, db_file=":memory:",
                    jersey_max_attempts=30, ocr_every_n_frames=10,
                    name_min_box_size=350, min_box_size=100,
                    team_ids=allowed_teams,
                )
            else:
                logger.info("Cached EasyOCR weights unavailable; player identities remain Unknown")
        except Exception as exc:
            # Detection remains valid, but all player IDs safely stay Unknown.
            logger.warning("Roster OCR unavailable for video %s; tracks remain Unknown: %s", video_id, type(exc).__name__)

        cap.release()
        cap = None
        video.status = "PROCESSING"
        video.error_message = None
        video.fps, video.width, video.height = fps, width, height
        if declared_frames > 0:
            video.total_frames = declared_frames
            video.duration_sec = declared_frames / fps
        match.status = "PROCESSING"
        db.commit()

        histories: Dict[int, deque] = defaultdict(lambda: deque(maxlen=12))
        track_rows: Dict[int, PlayerTrack] = {}
        track_stats: Dict[int, dict] = {}
        collision_streak: Dict[Tuple[int, int], int] = defaultdict(int)
        collision_active: Set[Tuple[int, int]] = set()
        fall_last_frame: Dict[int, int] = {}
        fall_horizontal_streak: Dict[int, int] = defaultdict(int)
        fall_active: Set[int] = set()
        frame_number = 0
        pre_frames = max(1, int(2.0 * fps))
        post_frames = max(1, int(2.0 * fps))
        pre_buffer = deque(maxlen=pre_frames)
        clips = []
        event_count = 0
        collision_count = 0

        def persist_alert(event_row, team_ids, title, message):
            targets = sorted({tid for tid in team_ids if tid is not None})
            if not targets:
                targets = [None]
            for team_id in targets:
                target_key = str(team_id) if team_id is not None else "unassigned"
                dedupe = f"ai-event:{'injury' if isinstance(event_row, InjuryEvent) else 'collision'}:{event_row.id}:team:{target_key}"
                if db.query(Alert.id).filter(Alert.dedupe_key == dedupe).first():
                    continue
                db.add(Alert(
                    injury_event_id=event_row.id if isinstance(event_row, InjuryEvent) else None,
                    collision_event_id=event_row.id if isinstance(event_row, CollisionEvent) else None,
                    recipient_user_id=None, team_id=team_id, alert_type="NEW_EVENT",
                    severity=event_row.risk, channel="web", title=title,
                    message=message, status="PENDING", dedupe_key=dedupe,
                ))

        def begin_clip(row, current_frame):
            small = cv2.resize(current_frame, (854, max(2, int(current_frame.shape[0] * 854 / current_frame.shape[1]))))
            small = small[:small.shape[0] - (small.shape[0] % 2), :small.shape[1] - (small.shape[1] % 2)]
            clips.append({"row": row, "frames": list(pre_buffer) + [small], "remaining": post_frames})

        def emit_clip_frames(current_frame):
            small = cv2.resize(current_frame, (854, max(2, int(current_frame.shape[0] * 854 / current_frame.shape[1]))))
            small = small[:small.shape[0] - (small.shape[0] % 2), :small.shape[1] - (small.shape[1] % 2)]
            pre_buffer.append(small)
            for clip in list(clips):
                clip["frames"].append(small.copy())
                clip["remaining"] -= 1
                if clip["remaining"] <= 0:
                    save_clip(clip)
                    clips.remove(clip)

        def save_clip(clip):
            frames = clip["frames"]
            if len(frames) < 2:
                return
            filename = f"video{video_id}_event{clip['row'].id}_{uuid.uuid4().hex[:8]}.webm"
            target = Path(settings.CLIPS_DIR) / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            h, w = frames[0].shape[:2]
            writer = cv2.VideoWriter(str(target), cv2.VideoWriter_fourcc(*"VP80"), fps, (w, h))
            if not writer.isOpened():
                writer.release()
                raise RuntimeError(f"WebM VP8 writer could not be opened for video {video_id}")
            try:
                for img in frames:
                    if img.shape[:2] != (h, w):
                        img = cv2.resize(img, (w, h))
                    writer.write(img)
            finally:
                writer.release()
            if not target.is_file() or target.stat().st_size == 0:
                raise RuntimeError(f"WebM clip was not written for video {video_id}")
            clip["row"].clip_path = filename
            db.commit()

        # One frame at a time preserves the real frame number and provides a small
        # pre/post incident buffer for authorized, signed clip URLs.
        infer_cap = cv2.VideoCapture(str(source))
        try:
            while True:
                ok, frame = infer_cap.read()
                if not ok:
                    break
                result = model.track(
                    frame, persist=True, tracker=str(tracker_cfg), device="cpu",
                    imgsz=640, conf=0.35, classes=[0], verbose=False,
                )[0]
                frame_number += 1
                boxes = result.boxes
                pose = result.keypoints
                people: Dict[int, dict] = {}
                if boxes is not None and boxes.id is not None and pose is not None and pose.data is not None:
                    box_xyxy = boxes.xyxy.cpu().numpy()
                    ids = boxes.id.int().cpu().tolist()
                    box_conf = boxes.conf.cpu().tolist()
                    points = pose.xy.cpu().numpy()
                    point_conf = pose.conf.cpu().numpy() if pose.conf is not None else np.zeros(points.shape[:2])
                    for idx, raw_track in enumerate(ids):
                        track_id = int(raw_track)
                        coords = [float(v) for v in box_xyxy[idx]]
                        kps = points[idx]
                        kconf = point_conf[idx]
                        valid = int(np.count_nonzero(kconf >= 0.35))
                        detection_conf = float(box_conf[idx])
                        if valid < 5 or detection_conf < 0.45:
                            continue
                        identity = _identity_payload(identifier, track_id, frame, coords, frame_number, kps, kconf, allowed_teams)
                        row = track_rows.get(track_id)
                        if row is None:
                            row = db.query(PlayerTrack).filter(
                                PlayerTrack.match_session_id == match.id,
                                PlayerTrack.track_id == track_id,
                            ).first()
                            if row is None:
                                row = PlayerTrack(match_session_id=match.id, video_id=video.id, track_id=track_id,
                                                  identity_status="UNKNOWN", first_frame=frame_number,
                                                  first_seen_sec=(frame_number - 1) / fps)
                                db.add(row)
                                db.flush()
                            elif row.video_id not in (None, video.id):
                                raise RuntimeError(
                                    "This match already has tracker IDs from another video; the current schema allows one tracker run per match"
                                )
                            track_rows[track_id] = row
                        row.last_frame = frame_number
                        row.last_seen_sec = (frame_number - 1) / fps
                        row.tracking_confidence = max(float(row.tracking_confidence or 0), detection_conf)
                        if identity is not None:
                            player = db.query(Player).filter(Player.id == identity["player_id"], Player.active.is_(True)).first()
                            if player is not None and player.team_id == identity["team_id"]:
                                row.player_id = player.id
                                row.team_id = player.team_id
                                row.identity_status = "CONFIRMED"
                                row.identified_by = identity["method"]
                                row.id_detail = identity["detail"]
                                row.id_confidence = identity["confidence"]
                                row.identified_at = datetime.now(timezone.utc)
                                predecessor_track_id = identity.get("predecessor_track_id")
                                if predecessor_track_id is not None:
                                    predecessor = track_rows.get(predecessor_track_id)
                                    if predecessor is None:
                                        predecessor = db.query(PlayerTrack).filter(
                                            PlayerTrack.match_session_id == match.id,
                                            PlayerTrack.track_id == predecessor_track_id,
                                        ).first()
                                    if (predecessor is not None and predecessor.id != row.id
                                            and predecessor.player_id == player.id):
                                        row.predecessor_id = predecessor.id
                        people[track_id] = {
                            "box": coords, "box_conf": detection_conf,
                            "points": kps, "kp_conf": kconf,
                        }
                        hist = histories[track_id]
                        hist.append({"frame": frame_number, "box": coords, "points": kps.copy(),
                                     "kp_conf": kconf.copy(), "box_conf": detection_conf})
                        width_box = max(1.0, coords[2] - coords[0])
                        height_box = max(1.0, coords[3] - coords[1])
                        # COCO shoulders/hips (5,6,11,12): torso angle from vertical.
                        if len(kps) >= 17 and min(kconf[5], kconf[6], kconf[11], kconf[12]) >= 0.35:
                            sh = (kps[5] + kps[6]) / 2.0
                            hip = (kps[11] + kps[12]) / 2.0
                            dx, dy = float(sh[0] - hip[0]), float(sh[1] - hip[1])
                            torso_angle = abs(float(np.degrees(np.arctan2(dx, -dy))))
                        else:
                            torso_angle = None
                        horizontal = width_box / height_box > 1.2 and torso_angle is not None and torso_angle > 55
                        fall_horizontal_streak[track_id] = fall_horizontal_streak[track_id] + 1 if horizontal else 0
                        seen_standing = any(
                            (h["box"][3] - h["box"][1]) > 1.15 * max(1, h["box"][2] - h["box"][0])
                            and h["frame"] >= frame_number - int(1.5 * fps)
                            for h in list(hist)[:-1]
                        )
                        cooldown = int(5 * fps)
                        if (seen_standing and fall_horizontal_streak[track_id] >= 2
                                and frame_number - fall_last_frame.get(track_id, -cooldown) >= cooldown
                                and track_id not in fall_active):
                            # Region only comes from a clearly displaced joint group over
                            # multiple frames; otherwise leave it null.
                            region = None
                            deviations = {}
                            if len(hist) >= 3:
                                before, now = hist[-3], hist[-1]
                                groups = {"HEAD": [0, 1, 2, 3, 4], "SHOULDER": [5, 6],
                                          "ARM": [7, 8, 9, 10], "HIP": [11, 12],
                                          "LOWER LEG": [13, 14, 15, 16]}
                                deviations = {}
                                norm = max(1.0, now["box"][3] - now["box"][1])
                                for group, indices in groups.items():
                                    moves = [float(np.linalg.norm(now["points"][j] - before["points"][j])) / norm
                                             for j in indices if before["kp_conf"][j] >= .5 and now["kp_conf"][j] >= .5]
                                    if moves:
                                        deviations[group] = sum(moves) / len(moves)
                                if deviations and max(deviations.values()) >= .18:
                                    region = max(deviations, key=deviations.get)
                            # confidence is detector/temporal evidence; risk is separately
                            # derived from the posture transition and never a diagnosis.
                            confidence = min(1.0, (sum(h["box_conf"] for h in list(hist)[-3:]) / min(3, len(hist)))
                                             * min(1.0, fall_horizontal_streak[track_id] / 3.0))
                            risk_score = min(1.0, 0.35 + 0.15 * fall_horizontal_streak[track_id]
                                             + (0.2 if region else 0.0))
                            risk, risk_score = _classify_fall_risk(
                                risk_score, region, confidence, seen_standing,
                                fall_horizontal_streak[track_id], deviations.get("HEAD"),
                            )
                            identity_row = row if row.player_id is not None else None
                            dedupe_key = build_fall_dedupe_key(
                                match.id, track_id, (frame_number - 1) / fps,
                                identity_row.player_id if identity_row else None,
                            )
                            existing_event = db.query(InjuryEvent).filter(
                                InjuryEvent.match_session_id == match.id,
                                InjuryEvent.dedupe_key == dedupe_key,
                            ).first() if dedupe_key else None
                            if existing_event is not None:
                                fall_last_frame[track_id] = frame_number
                                fall_active.add(track_id)
                                continue
                            collision_window = float(getattr(settings, "COLLISION_TO_INJURY_WINDOW_SEC", 8.0))
                            collision_id = _preceding_collision_id(
                                db, CollisionEvent, match.id, video.id,
                                identity_row.player_id if identity_row else None,
                                track_id, (frame_number - 1) / fps, collision_window,
                            )
                            event = InjuryEvent(
                                match_session_id=match.id, video_id=video.id,
                                player_id=identity_row.player_id if identity_row else None,
                                player_track_id=row.id, track_id=track_id,
                                collision_event_id=collision_id,
                                event_type="FALL", region=region, risk=risk,
                                confidence=confidence, risk_score=risk_score,
                                movement=float(torso_angle) if torso_angle is not None else None,
                                note=(
                                    f"Possible fall movement pattern across frames "
                                    f"{max(1, frame_number - fall_horizontal_streak[track_id] + 1)}-{frame_number} "
                                    f"({max(0.0, (frame_number - fall_horizontal_streak[track_id]) / fps):.2f}-"
                                    f"{(frame_number - 1) / fps:.2f} sec); prior standing posture observed; "
                                    f"{fall_horizontal_streak[track_id]} consecutive horizontal-posture frames; "
                                    f"strongest displaced region: {region or 'not established'}. "
                                    "AI screening signal, not a confirmed diagnosis."
                                ),
                                injury_note="AI-suggested screening categories, NOT a confirmed diagnosis. Clinical review recommended.",
                                source="ai", video_time_sec=(frame_number - 1) / fps,
                                frame_number=frame_number,
                                location_x=min(1.0, max(0.0, (coords[0] + coords[2]) / (2 * width))),
                                location_y=min(1.0, max(0.0, (coords[1] + coords[3]) / (2 * height))),
                                id_confidence=row.id_confidence if identity_row else None,
                                identified_by=row.identified_by if identity_row else None,
                                id_detail=row.id_detail if identity_row else None,
                                dedupe_key=dedupe_key,
                            )
                            db.add(event)
                            db.flush()
                            event_count += 1
                            fall_last_frame[track_id] = frame_number
                            fall_active.add(track_id)
                            team_id = identity_row.team_id if identity_row else None
                            if risk in ("MEDIUM", "HIGH", "CRITICAL"):
                                persist_alert(event, [team_id], "Possible movement concern",
                                              "A video screening signal requires appropriate review. This is not a diagnosis.")
                            if collision_id is not None:
                                related_collision = db.query(CollisionEvent).filter(
                                    CollisionEvent.id == collision_id,
                                ).first()
                                if related_collision is not None:
                                    related_collision.risk, related_collision.risk_score = _classify_collision_risk(
                                        float(related_collision.iou or 0.0),
                                        int(related_collision.contact_frames or 0),
                                        [event],
                                    )
                                    related_collision.note = (
                                        (related_collision.note or "Sustained tracked contact.")
                                        + f" Associated fall evidence: {risk} risk, "
                                        + f"{region or 'body region not established'} region at "
                                        + f"{(frame_number - 1) / fps:.2f} sec."
                                    )
                                    for participant in related_collision.participants:
                                        if participant.track_id == track_id:
                                            participant.region = region
                                            participant.movement = event.movement
                                    if risk in ("MEDIUM", "HIGH", "CRITICAL"):
                                        persist_alert(
                                            related_collision,
                                            [participant.player.team_id if participant.player is not None else None
                                             for participant in related_collision.participants],
                                            "Possible player contact",
                                            "A contact event has corroborating fall evidence and requires review. "
                                            "This is not a diagnosis.",
                                        )
                            begin_clip(event, frame)

                # Contact must persist in adjacent frames. Merely appearing close
                # for one frame is not recorded as a collision.
                contact_now = set()
                ids_now = sorted(people)
                for i, left_id in enumerate(ids_now):
                    for right_id in ids_now[i + 1:]:
                        overlap = _iou(people[left_id]["box"], people[right_id]["box"])
                        pair = (left_id, right_id)
                        if overlap >= 0.2:
                            contact_now.add(pair)
                            collision_streak[pair] += 1
                            if collision_streak[pair] == 2 and pair not in collision_active:
                                left_row, right_row = track_rows[left_id], track_rows[right_id]
                                video_time_sec = (frame_number - 1) / fps
                                associated_injuries = db.query(InjuryEvent).filter(
                                    InjuryEvent.match_session_id == match.id,
                                    InjuryEvent.video_id == video.id,
                                    InjuryEvent.track_id.in_((left_id, right_id)),
                                    InjuryEvent.video_time_sec >= video_time_sec - 2.0,
                                    InjuryEvent.video_time_sec <= video_time_sec + 2.0,
                                    InjuryEvent.collision_event_id.is_(None),
                                ).all()
                                risk, risk_score = _classify_collision_risk(
                                    overlap, collision_streak[pair], associated_injuries,
                                )
                                score = min(
                                    people[left_id]["box_conf"],
                                    people[right_id]["box_conf"],
                                ) * min(1.0, collision_streak[pair] / 3.0)
                                boxes_pair = [people[left_id]["box"], people[right_id]["box"]]
                                bx1 = min(b[0] for b in boxes_pair); by1 = min(b[1] for b in boxes_pair)
                                bx2 = max(b[2] for b in boxes_pair); by2 = max(b[3] for b in boxes_pair)
                                collision_dedupe = build_collision_dedupe_key(
                                    match.id, left_id, right_id, (frame_number - 1) / fps,
                                )
                                existing_collision = db.query(CollisionEvent).filter(
                                    CollisionEvent.match_session_id == match.id,
                                    CollisionEvent.dedupe_key == collision_dedupe,
                                ).first() if collision_dedupe else None
                                if existing_collision is not None:
                                    collision_active.add(pair)
                                    continue
                                collision = CollisionEvent(
                                    match_session_id=match.id, video_id=video.id, frame_number=frame_number,
                                    video_time_sec=video_time_sec, iou=overlap,
                                    contact_frames=collision_streak[pair], confidence=score,
                                    risk=risk, risk_score=risk_score, event_type="COLLISION", source="ai",
                                    location_x=min(1.0, max(0.0, (bx1 + bx2) / (2 * width))),
                                    location_y=min(1.0, max(0.0, (by1 + by2) / (2 * height))),
                                    location_bbox=[bx1 / width, by1 / height, bx2 / width, by2 / height],
                                    note=(
                                        f"Sustained tracked contact over {collision_streak[pair]} frames "
                                        f"(box overlap {overlap:.2f}); "
                                        + (
                                            "temporally associated fall evidence; collision severity follows "
                                            "the fall screening result. Not a diagnosis."
                                            if associated_injuries else
                                            "no associated fall evidence; contact alone is LOW risk and is not "
                                            "classified as an injury."
                                        )
                                    ),
                                    dedupe_key=collision_dedupe,
                                )
                                left_pid, right_pid = left_row.player_id, right_row.player_id
                                if left_pid is not None and left_pid == right_pid:
                                    # One person cannot be both participants. Keep the higher-confidence
                                    # claim and leave the other participant UNKNOWN, rather than duplicating
                                    # a player (which would also trip the unique-player constraint and lose
                                    # the whole collision). The track id and its evidence are still stored.
                                    if (left_row.id_confidence or 0.0) >= (right_row.id_confidence or 0.0):
                                        right_pid = None
                                    else:
                                        left_pid = None
                                for slot_no, part_id, part_row, part_pid in (
                                    (1, left_id, left_row, left_pid),
                                    (2, right_id, right_row, right_pid),
                                ):
                                    collision.participants.append(CollisionParticipant(
                                        slot=slot_no, track_id=part_id, player_id=part_pid,
                                        player_track_id=part_row.id, id_confidence=part_row.id_confidence,
                                        identified_by=part_row.identified_by, id_detail=part_row.id_detail,
                                        identified_at=part_row.identified_at,
                                        region=next(
                                            (event.region for event in associated_injuries
                                             if event.track_id == part_id),
                                            None,
                                        ),
                                        movement=next(
                                            (event.movement for event in associated_injuries
                                             if event.track_id == part_id),
                                            None,
                                        ),
                                    ))
                                db.add(collision)
                                db.flush()
                                for injury in associated_injuries:
                                    injury.collision_event_id = collision.id
                                collision_count += 1
                                collision_active.add(pair)
                                if risk in ("MEDIUM", "HIGH", "CRITICAL"):
                                    persist_alert(collision, [left_row.team_id, right_row.team_id],
                                                  "Possible player contact",
                                                  "Video screening detected sustained player contact. This is not a diagnosis.")
                                begin_clip(collision, frame)
                for pair in list(collision_streak):
                    if pair not in contact_now:
                        collision_streak[pair] = 0
                        collision_active.discard(pair)
                for track_id in list(fall_active):
                    if track_id in people and not (people[track_id]["box"][2] - people[track_id]["box"][0]
                                                   > 1.15 * (people[track_id]["box"][3] - people[track_id]["box"][1])):
                        fall_active.discard(track_id)
                emit_clip_frames(frame)
                if frame_number % 100 == 0:
                    db.commit()
        finally:
            infer_cap.release()

        # A final short clip may be shorter than requested at the end of a video.
        for clip in clips:
            save_clip(clip)
        if frame_number == 0:
            raise RuntimeError("No decodable frames were found in the uploaded video")
        video = db.query(Video).filter(Video.id == video_id).with_for_update().first()
        match = db.query(MatchSession).filter(MatchSession.id == video.match_session_id).first()
        video.status = "PROCESSED"
        video.error_message = None
        video.processed_at = datetime.now(timezone.utc)
        video.total_frames = frame_number
        video.duration_sec = frame_number / fps
        match.status = "COMPLETED"
        db.commit()
        logger.info("processed video=%s frames=%s tracks=%s falls=%s collisions=%s",
                    video_id, frame_number, len(track_rows), event_count, collision_count)
    except Exception as exc:
        db.rollback()
        logger.exception("video processing failed for video %s", video_id)
        try:
            video = db.query(Video).filter(Video.id == video_id).first()
            if video is not None:
                match = db.query(MatchSession).filter(MatchSession.id == video.match_session_id).first()
                video.status = "FAILED"
                video.error_message = str(exc)[:2000]
                if match is not None:
                    match.status = "FAILED"
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("could not persist processing failure status for video %s", video_id)
    finally:
        if cap is not None:
            cap.release()
        db.close()
        with _lock:
            _active.discard(video_id)
