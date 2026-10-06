"""PlayerIdentifier: jersey number / surname OCR + face recognition, matched against the player roster."""

import os
import sys
import math
import sqlite3
import statistics
from collections import Counter
import numpy as np
import torch
import easyocr
import cv2
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from database import SessionLocal
from models import Player, Team

TEAM_KIT_COLORS_BGR = {
    "Portugal": (40, 20, 90),        # dark maroon/red shirt
    "Netherlands": (20, 120, 230),   # orange shirt
}

# If the shirt is not clearly closer to one kit, treat the team as unknown instead of guessing
TEAM_COLOR_MARGIN = 20
# Reject colors far from every known kit instead of assigning the nearest one.
TEAM_COLOR_MAX_DISTANCE = 110.0

# Goalkeepers wear a different kit from their team. A read of one of these numbers
# on a shirt that clearly matches an OUTFIELD kit is a misread (e.g. "17" read as "1").
GOALKEEPER_NUMBERS = {1}

# A single digit that is also part of a two-digit number in the same team
# (1 -> 17, 7 -> 17, 6 -> 16 ...) is often half of a bigger number.
# Such reads need this much evidence before they are trusted:
PARTIAL_DIGIT_MIN_VOTES = 5
PARTIAL_DIGIT_MIN_AVG_CONF = 0.80


# ============================================================
# FACE GALLERY FOLDER NAMES
# known_players\<folder>\photo.jpg — the folder can be named
#   "Cristiano Ronaldo", "17 - Cristiano Ronaldo", "17_Cristiano Ronaldo"
# Accents and capital letters don't matter ("Luis Figo" = "Luís Figo").
# ============================================================

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def plain_text(s):
    """Upper-case, accents removed, single spaces."""
    import unicodedata
    s = "".join(
        ch for ch in unicodedata.normalize("NFD", str(s).upper())
        if unicodedata.category(ch) != "Mn"
    )
    return " ".join(s.split())


def resolve_player_folder(folder_name, roster_by_plain_name):
    """Returns the roster name a known_players folder belongs to, or None."""

    candidates = [folder_name]

    stripped = folder_name.lstrip("0123456789").lstrip(" _-.")
    if stripped != folder_name:
        candidates.append(stripped)

    if " - " in folder_name:
        candidates.extend(part for part in folder_name.split(" - "))

    for text in candidates:
        key = plain_text(text)
        if key in roster_by_plain_name:
            return roster_by_plain_name[key]

    return None


class PlayerIdentifier:

    def __init__(
        self,
        known_players_dir=None,
        db_file=None,
        ocr_confidence_threshold=0.55,
        jersey_min_votes=3,
        jersey_max_attempts=150,  # ~5 seconds at 30fps, not <1 second
        face_distance_threshold=0.30,   # Facenet512 cosine distance; lower = stricter
        face_min_votes=3,               # face matches needed on one track before trusting it
        face_margin=0.05,               # best player must beat the next-best player by this much
        face_max_attempts=120,          # face attempts per track before giving up
        face_every_n_frames=5,          # face is slow — try it on every Nth frame per track
        face_min_box_height=150,        # standing player box must be at least this tall (px)
        face_detector="opencv",         # DeepFace detector: "opencv" (fast) or "retinaface" (better, slower)
        face_model_name="Facenet512",
        enable_face=True,
        auto_face_gallery=True,         # learn faces automatically from players identified by jersey
        auto_face_min_confidence=0.85,  # only learn from jersey reads at least this sure
        auto_face_per_player=8,         # max auto-learned faces per player
        auto_face_every_n_frames=10,    # how often to try grabbing a face from an identified track
        auto_face_min_face_px=28,       # face must be at least this tall in the original frame
        jersey_crop_upscale=2.0,
        min_box_size=90,            # skip OCR when the box's longest side is under this many pixels
        ocr_every_n_frames=2,       # attempt OCR on every Nth frame per track (speed)
        name_min_box_size=160,      # only try to read surnames on boxes at least this big (close-ups)
        retry_growth_factor=1.4,    # reopen a given-up track once its box is this much bigger
        handoff_enabled=True,       # carry identity to a new track that appears where a known player was lost
        handoff_max_gap_frames=60,  # ...if the old track was lost within this many frames (2 s at 30 fps)
        handoff_max_distance=1.0,   # ...and the boxes are closer than this many box-sizes
        handoff_min_votes=1,        # reads needed to confirm a hand-off (normal tracks need jersey_min_votes)
        duplicate_guard_frames=2,   # a live player may sit on only ONE track; block a second claim within this many frames
        team_ids=None,              # optional team IDs belonging to the current match
    ):
        self.ocr_confidence_threshold = ocr_confidence_threshold
        self.jersey_min_votes = jersey_min_votes
        self.jersey_max_attempts = jersey_max_attempts

        self.face_distance_threshold = face_distance_threshold
        self.face_min_votes = face_min_votes
        self.face_margin = face_margin
        self.face_max_attempts = face_max_attempts
        self.face_every_n_frames = face_every_n_frames
        self.face_min_box_height = face_min_box_height
        self.face_detector = face_detector
        self.face_model_name = face_model_name
        self.enable_face = enable_face

        self.auto_face_gallery = auto_face_gallery
        self.auto_face_min_confidence = auto_face_min_confidence
        self.auto_face_per_player = auto_face_per_player
        self.auto_face_every_n_frames = auto_face_every_n_frames
        self.auto_face_min_face_px = auto_face_min_face_px

        self.jersey_crop_upscale = jersey_crop_upscale

        self.min_box_size = min_box_size
        self.ocr_every_n_frames = ocr_every_n_frames
        self.name_min_box_size = name_min_box_size
        self.retry_growth_factor = retry_growth_factor
        self.handoff_enabled = handoff_enabled
        self.handoff_max_gap_frames = handoff_max_gap_frames
        self.handoff_max_distance = handoff_max_distance
        self.handoff_min_votes = handoff_min_votes
        self.duplicate_guard_frames = duplicate_guard_frames
        self.match_team_ids = {int(team_id) for team_id in (team_ids or ())}

        here = os.path.dirname(__file__)

        self.known_players_dir = known_players_dir or os.path.join(
            here, "..", "known_players"
        )

        self.db_file = db_file or os.path.join(here, "player_identifications.db")

        # faces learned automatically are saved here, so the next run starts with them
        self.auto_faces_dir = os.path.join(here, "..", "known_players_auto")

        # pose keypoints of the box currently being read (set by identify / remember_crop)
        self._active_kps = None

        print("[PlayerIdentifier] Loading player roster from database...")
        self._load_roster()

        print("[PlayerIdentifier] Loading jersey OCR model...")
        self._ocr_reader = easyocr.Reader(['en'], gpu=torch.cuda.is_available())

        self._face_gallery = []
        self._auto_face_count = {}      # player_id -> faces learned (this run + saved)
        self._auto_face_tracks = {}     # track_id -> faces learned from this track

        if self.enable_face:
            print("[PlayerIdentifier] Building face gallery...")
            self._build_face_gallery(self.known_players_dir, "photos")
            self._build_face_gallery(self.auto_faces_dir, "auto-learned")

        self._init_db()

        self._confirmed = {}          # track_id -> result dict
        self._jersey_votes = {}       # track_id -> {number: [conf, ...]}
        self._jersey_attempts = {}    # track_id -> int
        self._jersey_gave_up = set()
        self._face_votes = {}         # track_id -> {name: [dist, ...]}
        self._face_attempts = {}      # track_id -> int
        self._face_gave_up = set()

        self._first_seen = {}         # track_id -> first frame_index seen
        self._last_seen = {}          # track_id -> (frame_index, box)
        self._team_of = {}            # confirmed track_id -> team_id (for hand-off)
        self._gave_up_size = {}       # track_id -> box size when OCR gave up
        self._handoff_prior = {}      # track_id -> candidate identity carried from a lost track
        self._confirm_frames = []     # frames from first sight to identification

        # recent jersey crops per track, read at fall time (see identify_from_history)
        self._crop_history = {}       # track_id -> [(frame_index, crop, team_id), ...]
        self.history_every_n_frames = 3
        self.history_max_crops = 12
        self.history_forget_frames = 450
        self._color_debug_count = 0

        self._stats = {
            "skipped_small": 0, "skipped_cadence": 0, "reopened": 0,
            "handoff_candidates": 0, "handoff_confirmed": 0, "unmatched_reads": 0,
            "face_attempts": 0, "face_not_found": 0, "face_rejected_team": 0,
            "pose_crops": 0, "box_crops": 0, "auto_faces_learned": 0,
            "rejected_partial": 0, "duplicate_blocked": 0,
        }

        print("[PlayerIdentifier] Ready.")

    # ------------------------------------------------------------
    # ROSTER
    # ------------------------------------------------------------

    def _load_roster(self):

        db = SessionLocal()
        players = db.query(Player).filter(Player.active.is_(True)).all()
        teams = db.query(Team).all()
        db.close()

        if self.match_team_ids:
            players = [p for p in players if p.team_id in self.match_team_ids]
            teams = [t for t in teams if t.id in self.match_team_ids]

        self.teams_by_id = {t.id: t.name for t in teams}
        self._roster_team_ids = {p.team_id for p in players}

        self._jersey_name_attempts = {}
        self._jersey_name_votes = {}
        self._jersey_name_gave_up = set()

        self.roster_by_jersey_multi = {}

        for p in players:
            self.roster_by_jersey_multi.setdefault(p.jersey_number, []).append(
                (p.id, p.name, p.team_id)
            )

        self.roster_by_name = {p.name: p.id for p in players}
        self._number_by_player = {p.id: p.jersey_number for p in players}
        self._team_by_player = {p.id: p.team_id for p in players}
        self._name_by_player = {p.id: p.name for p in players}
        self._roster_by_plain_name = {plain_text(p.name): p.name for p in players}

        print("[PlayerIdentifier] Loaded", len(players), "players across", len(teams), "teams")

    def _match_roster(self, number, team_id):
        """Match within this roster; ambiguous numbers require a confident team match."""

        candidates = self.roster_by_jersey_multi.get(number, [])

        if not candidates:
            return None

        if team_id is not None:

            for pid, name, tid in candidates:
                if tid == team_id:
                    return (pid, name)

            return None   # number exists, but only for the other team

        if len(candidates) == 1:
            return candidates[0][:2]

        return None

    def _is_partial_digit(self, number, team_id):
        """True if a single-digit read could be half of a two-digit roster number."""

        if number is None or number >= 10:
            return False

        digit = str(number)

        for roster_number, players in self.roster_by_jersey_multi.items():

            if roster_number < 10 or digit not in str(roster_number):
                continue

            for _, _, tid in players:
                if team_id is None or tid == team_id:
                    return True

        return False

    def _has_longer_read(self, number, votes):
        """True if this track also has two-digit reads containing this digit (e.g. 17 when reading 1)."""

        digit = str(number)

        return any(
            other >= 10 and digit in str(other) and len(confs) > 0
            for other, confs in votes.items()
        )

    def _goalkeeper_misread(self, number, team_id):
        """A goalkeeper number on a shirt that clearly matches an outfield kit -> misread."""

        return number in GOALKEEPER_NUMBERS and team_id is not None

    # ------------------------------------------------------------
    # TEAM COLOUR
    # ------------------------------------------------------------

    def _guess_team_id(self, frame, box):
        """Use legacy kit colors only when both teams are in this roster; otherwise return unknown."""

        x1, y1, x2, y2 = [int(v) for v in box]
        box_h, box_w = y2 - y1, x2 - x1

        if box_h <= 0 or box_w <= 0:
            return None

        top = y1 + int(0.15 * box_h)
        bottom = y1 + int(0.55 * box_h)
        left = x1 + int(0.25 * box_w)
        right = x2 - int(0.25 * box_w)

        fh, fw = frame.shape[:2]
        top, left = max(0, top), max(0, left)
        bottom, right = min(fh, bottom), min(fw, right)

        if bottom <= top or right <= left:
            return None

        patch = frame[top:bottom, left:right]

        if patch.size == 0:
            return None

        avg_color = patch.reshape(-1, 3).mean(axis=0)

        # First 30 samples are printed so you can check TEAM_KIT_COLORS_BGR against the real video
        if self._color_debug_count < 30:
            self._color_debug_count += 1
            print("[TEAM-COLOR] shirt avg BGR:", [int(v) for v in avg_color])

        ranked = sorted(
            (float(np.linalg.norm(avg_color - np.array(ref))), name)
            for name, ref in TEAM_KIT_COLORS_BGR.items()
        )

        best_dist, best_team_name = ranked[0]

        if best_dist > TEAM_COLOR_MAX_DISTANCE:
            return None

        if len(ranked) > 1 and ranked[1][0] - best_dist < TEAM_COLOR_MARGIN:
            return None

        legacy_team_ids = {
            name: team_id
            for team_id, name in self.teams_by_id.items()
            if team_id in self._roster_team_ids
        }
        if not all(name in legacy_team_ids for name in TEAM_KIT_COLORS_BGR):
            return None

        return legacy_team_ids.get(best_team_name)

    # ------------------------------------------------------------
    # FACE GALLERY / LOG DB
    # ------------------------------------------------------------

    @property
    def face_ready(self):
        """True when face recognition is on and at least one reference face loaded."""
        return self.enable_face and len(self._face_gallery) > 0

    @staticmethod
    def _largest_face(reps):
        """DeepFace can return several faces in one image — keep the biggest."""

        def area(r):
            fa = r.get("facial_area") or {}
            return fa.get("w", 0) * fa.get("h", 0)

        return max(reps, key=area) if reps else None

    def _build_face_gallery(self, folder_root, label):

        if not os.path.isdir(folder_root):
            print("[PlayerIdentifier] No", label, "face folder yet at", os.path.abspath(folder_root))
            return

        from deepface import DeepFace

        per_player = {}

        for folder in sorted(os.listdir(folder_root)):

            player_dir = os.path.join(folder_root, folder)

            if not os.path.isdir(player_dir):
                continue

            roster_name = resolve_player_folder(folder, self._roster_by_plain_name)

            if roster_name is None:
                print("[PlayerIdentifier] Face folder not in roster, skipped:", folder)
                continue

            player_id = self.roster_by_name[roster_name]

            for filename in sorted(os.listdir(player_dir)):

                if not filename.lower().endswith(IMAGE_EXTENSIONS):
                    continue

                filepath = os.path.join(player_dir, filename)

                try:
                    reps = DeepFace.represent(
                        img_path=filepath,
                        model_name=self.face_model_name,
                        detector_backend=self.face_detector,
                        enforce_detection=True,
                    )
                    face = self._largest_face(reps)
                    self._face_gallery.append((player_id, roster_name, face["embedding"]))
                    per_player[roster_name] = per_player.get(roster_name, 0) + 1

                    if label == "auto-learned":
                        self._auto_face_count[player_id] = self._auto_face_count.get(player_id, 0) + 1

                except Exception as e:
                    print("[PlayerIdentifier] No usable face in", filepath, "-", str(e).splitlines()[0])

        for name in sorted(per_player):
            print("[PlayerIdentifier]  ", label, "faces:", per_player[name], "-", name)

        print(
            "[PlayerIdentifier] Face gallery (" + label + "):", sum(per_player.values()),
            "faces for", len(per_player), "players"
        )

    def _init_db(self):

        # A per-connection SQLite :memory: database disappears when this method returns.
        if self.db_file == ":memory:":
            return

        conn = sqlite3.connect(self.db_file)
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS identifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL,
                player_id INTEGER,
                player_name TEXT,
                method TEXT NOT NULL,
                detail TEXT,
                confidence REAL,
                vote_count INTEGER,
                timestamp TEXT NOT NULL
            )
        """)

        conn.commit()
        conn.close()

    def _log(self, track_id, result):

        if self.db_file == ":memory:":
            return

        conn = sqlite3.connect(self.db_file)
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO identifications
            (track_id, player_id, player_name, method, detail, confidence, vote_count, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                track_id, result["player_id"], result["player_name"],
                result["method"], result["detail"], result["confidence"],
                result["vote_count"], datetime.now().isoformat()
            )
        )

        conn.commit()
        conn.close()

    # ------------------------------------------------------------
    # JERSEY NUMBER OCR
    # ------------------------------------------------------------

    def _crop_torso_from_pose(self, frame):
        """Tight crop between the shoulders and hips (where the number is), or None."""

        if self._active_kps is None:
            return None

        points, conf = self._active_kps

        try:
            idx = (5, 6, 11, 12)   # left/right shoulder, left/right hip
            if min(float(conf[i]) for i in idx) < 0.35:
                return None

            xs = [float(points[i][0]) for i in idx]
            ys = [float(points[i][1]) for i in idx]

        except Exception:
            return None

        if min(xs) <= 0 or min(ys) <= 0:
            return None

        shoulder_y = (ys[0] + ys[1]) / 2
        hip_y = (ys[2] + ys[3]) / 2
        torso_h = hip_y - shoulder_y

        # upright torso only — lying players fall back to the box crop
        if torso_h < 20:
            return None

        width = max(xs) - min(xs)
        pad_x = max(0.20 * width, 0.10 * torso_h)

        top = int(shoulder_y - 0.05 * torso_h)
        bottom = int(hip_y - 0.05 * torso_h)
        left = int(min(xs) - pad_x)
        right = int(max(xs) + pad_x)

        fh, fw = frame.shape[:2]
        top, left = max(0, top), max(0, left)
        bottom, right = min(fh, bottom), min(fw, right)

        if bottom - top < 12 or right - left < 12:
            return None

        return frame[top:bottom, left:right]

    def _crop_jersey_region(self, frame, box):

        pose_crop = self._crop_torso_from_pose(frame)

        if pose_crop is not None:
            self._stats["pose_crops"] += 1
            return pose_crop

        self._stats["box_crops"] += 1

        x1, y1, x2, y2 = [int(v) for v in box]
        box_h, box_w = y2 - y1, x2 - x1

        if box_h <= 0 or box_w <= 0:
            return None

        top = y1 + int(0.15 * box_h)
        bottom = y1 + int(0.55 * box_h)
        left = x1 + int(0.15 * box_w)
        right = x2 - int(0.15 * box_w)

        fh, fw = frame.shape[:2]
        top, left = max(0, top), max(0, left)
        bottom, right = min(fh, bottom), min(fw, right)

        if bottom <= top or right <= left:
            return None

        return frame[top:bottom, left:right]

    def _read_jersey_number(self, frame, box):

        crop = self._crop_jersey_region(frame, box)

        return self._read_number_from_crop(crop)

    def _read_number_from_crop(self, crop):
        """OCR on a jersey crop: bigger, contrast-boosted, 1-2 digits only, joins split digits like 1 + 8."""

        if crop is None or crop.size == 0:
            return None, 0.0

        h = crop.shape[0]
        scale = min(4.0, max(self.jersey_crop_upscale, 160.0 / max(h, 1)))

        big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
        enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(gray)

        best_number, best_conf = None, 0.0

        for variant in (big, enhanced):

            try:
                results = self._ocr_reader.readtext(variant, allowlist="0123456789")
            except Exception:
                continue

            singles = []

            for bbox, text, conf in results:

                text = text.strip()

                if not text.isdigit() or len(text) > 2:
                    continue

                number = int(text)

                if number < 1 or number > 99:
                    continue

                if conf > best_conf:
                    best_number, best_conf = number, float(conf)

                if len(text) == 1:
                    ys = [p[1] for p in bbox]
                    singles.append((
                        min(p[0] for p in bbox),
                        sum(ys) / 4.0,
                        max(ys) - min(ys),
                        text,
                        float(conf),
                    ))

            # two separate single digits side by side -> one two-digit number
            if len(singles) == 2:

                (xa, ya, ha, ta, ca), (xb, yb, hb, tb, cb) = sorted(singles)

                if abs(ya - yb) < 0.5 * max(ha, hb):

                    joined = int(ta + tb)
                    joined_conf = min(ca, cb)

                    if 1 <= joined <= 99 and joined_conf > best_conf:
                        best_number, best_conf = joined, joined_conf

        return best_number, best_conf

    def _read_jersey_name(self, frame, box):
        """Separate OCR pass, letters allowed (unlike the number-only pass)."""

        crop = self._crop_jersey_region(frame, box)

        if crop is None or crop.size == 0:
            return None, 0.0

        if self.jersey_crop_upscale and self.jersey_crop_upscale != 1.0:
            crop = cv2.resize(
                crop, None,
                fx=self.jersey_crop_upscale, fy=self.jersey_crop_upscale,
                interpolation=cv2.INTER_CUBIC
            )

        try:
            results = self._ocr_reader.readtext(crop)
        except Exception:
            return None, 0.0

        if not results:
            return None, 0.0

        best = max(results, key=lambda r: r[2])
        text, conf = best[1], best[2]

        if not text.isalpha() or len(text) < 3:
            return None, 0.0

        return text.upper(), float(conf)

    def _match_name_text(self, ocr_text):
        """Returns a player name only when the text points to exactly ONE roster player."""

        import unicodedata

        if len(ocr_text) < 4:
            return None

        def plain(s):
            return "".join(
                ch for ch in unicodedata.normalize("NFD", s.upper())
                if unicodedata.category(ch) != "Mn"
            )

        target = plain(ocr_text)

        matches = [
            full_name for full_name in self.roster_by_name
            if target in plain(full_name)
        ]

        if len(matches) == 1:
            return matches[0]

        return None

    @staticmethod
    def _box_size(box):
        return max(float(box[2]) - float(box[0]), float(box[3]) - float(box[1]))

    def _try_jersey_name(self, track_id, frame, box, prior=None):

        if track_id in self._jersey_name_gave_up:
            return None

        text, conf = self._read_jersey_name(frame, box)

        self._jersey_name_attempts[track_id] = self._jersey_name_attempts.get(track_id, 0) + 1

        if text is not None and conf >= self.ocr_confidence_threshold:

            matched_name = self._match_name_text(text)

            if matched_name is not None:

                votes = self._jersey_name_votes.setdefault(track_id, {})
                votes.setdefault(matched_name, []).append(conf)

                player_id = self.roster_by_name.get(matched_name)
                handoff = prior is not None and player_id == prior["player_id"]
                needed = self.handoff_min_votes if handoff else self.jersey_min_votes

                if len(votes[matched_name]) >= needed:

                    confs = votes[matched_name]

                    return {
                        "player_id": player_id,
                        "player_name": matched_name,
                        "method": "jersey_handoff" if handoff else "jersey_name",
                        "detail": f"jersey text '{text}'"
                                  + (f" (carried from track {prior['from_track']})" if handoff else ""),
                        "confidence": round(sum(confs) / len(confs), 3),
                        "vote_count": len(confs),
                    }

        if self._jersey_name_attempts[track_id] >= self.jersey_max_attempts:
            self._jersey_name_gave_up.add(track_id)
            self._gave_up_size.setdefault(track_id, self._box_size(box))

        return None

    def _try_jersey(self, track_id, frame, box, prior=None):

        if track_id in self._jersey_gave_up:
            return None

        number, conf = self._read_jersey_number(frame, box)
        if number is not None:
            print(f"[DEBUG-OCR] track {track_id} read #{number} conf {conf:.2f} box {int(self._box_size(box))}px")

        self._jersey_attempts[track_id] = self._jersey_attempts.get(track_id, 0) + 1

        if number is not None and conf >= self.ocr_confidence_threshold:

            votes = self._jersey_votes.setdefault(track_id, {})
            votes.setdefault(number, []).append(conf)

            confs = votes[number]
            avg = sum(confs) / len(confs)

            handoff = prior is not None and prior["jersey_number"] == number

            team_id = None if handoff else self._guess_team_id(frame, box)

            if not handoff and self._goalkeeper_misread(number, team_id):
                self._stats["rejected_partial"] += 1
                print(f"[DEBUG-OCR] track {track_id} read #{number} on an outfield shirt - goalkeeper number, ignored")
                votes.pop(number, None)
                number = None

            if number is None:
                enough = False

            elif handoff:
                enough = len(confs) >= self.handoff_min_votes

            elif self._is_partial_digit(number, team_id):
                # "1" could be half of "17": needs many strong reads and no longer read
                enough = (
                    len(confs) >= PARTIAL_DIGIT_MIN_VOTES
                    and avg >= PARTIAL_DIGIT_MIN_AVG_CONF
                    and not self._has_longer_read(number, votes)
                )

            else:
                # Require independent frame evidence before confirming a jersey read.
                enough = (
                    len(confs) >= self.jersey_min_votes
                    or (len(confs) >= 2 and avg >= 0.70)
                )

            if enough:

                if handoff:
                    match = (prior["player_id"], prior["player_name"])
                else:
                    match = self._match_roster(number, team_id)

                if match is not None:

                    return {
                        "player_id": match[0],
                        "player_name": match[1],
                        "method": "jersey_handoff" if handoff else "jersey",
                        "detail": "jersey #" + str(number)
                                  + (f" (carried from track {prior['from_track']})" if handoff else ""),
                        "confidence": round(avg, 3),
                        "vote_count": len(confs),
                        **({"from_track": prior["from_track"]} if handoff else {}),
                    }

                self._stats["unmatched_reads"] += 1
                print(f"[DEBUG-OCR] track {track_id} read #{number} but no roster match (team unclear, other team, or number not in roster)")
                votes.pop(number, None)

        if self._jersey_attempts[track_id] >= self.jersey_max_attempts:
            self._jersey_gave_up.add(track_id)
            self._gave_up_size.setdefault(track_id, self._box_size(box))

        return None

    # ------------------------------------------------------------
    # TRACK BOOKKEEPING
    # ------------------------------------------------------------

    def _touch(self, track_id, frame_index, box):
        self._first_seen.setdefault(track_id, frame_index)
        self._last_seen[track_id] = (frame_index, tuple(float(v) for v in box))

    def _maybe_reopen(self, track_id, box):
        """A track that gave up while its box was small gets a fresh try once the box grows."""

        if track_id in self._jersey_gave_up or track_id in self._jersey_name_gave_up:

            base_size = self._gave_up_size.get(track_id)

            if base_size and self._box_size(box) >= self.retry_growth_factor * base_size:
                self._jersey_gave_up.discard(track_id)
                self._jersey_name_gave_up.discard(track_id)
                self._jersey_attempts[track_id] = 0
                self._jersey_name_attempts[track_id] = 0
                self._gave_up_size.pop(track_id, None)
                self._stats["reopened"] += 1

    def _worth_reading(self, track_id, box, frame_index):

        if self._box_size(box) < self.min_box_size:
            self._stats["skipped_small"] += 1
            return False

        n = self.ocr_every_n_frames

        if frame_index is not None and n and n > 1 and (frame_index + track_id) % n != 0:
            self._stats["skipped_cadence"] += 1
            return False

        return True

    def _player_live_on_other_track(self, player_id, track_id, frame_index):
        """True when a DIFFERENT track that is still live is already confirmed as this player.

        One person can occupy only one track at a time, so if two live tracks claim the
        same player then at least one is wrong (a duplicate track, or the tracker reused
        an id after a crossing/occlusion). The newer claim is refused and its identity
        stays UNKNOWN - a correct UNKNOWN is always preferred over a wrong player id.
        """

        if player_id is None or frame_index is None:
            return False

        window = self.duplicate_guard_frames

        for tid, res in self._confirmed.items():

            if tid == track_id or res.get("player_id") != player_id:
                continue

            seen = self._last_seen.get(tid)

            if seen is not None and 0 <= frame_index - seen[0] <= window:
                return True

        return False

    def _find_handoff_candidate(self, track_id, frame, box, frame_index):
        """Looks for an identified player who was lost a moment ago right where this new track appeared."""

        nx1, ny1, nx2, ny2 = [float(v) for v in box]
        new_size = max(nx2 - nx1, ny2 - ny1)
        cx, cy = (nx1 + nx2) / 2, (ny1 + ny2) / 2

        visible_now = set()
        for tid, res in self._confirmed.items():
            seen = self._last_seen.get(tid)
            if tid != track_id and seen and frame_index - seen[0] <= 2:
                visible_now.add(res["player_id"])

        new_team = None
        found = {}

        for tid, res in self._confirmed.items():

            if tid == track_id or res["player_id"] is None:
                continue

            seen = self._last_seen.get(tid)
            if not seen:
                continue

            gap = frame_index - seen[0]
            if gap <= 2 or gap > self.handoff_max_gap_frames:
                continue

            if res["player_id"] in visible_now:
                continue

            ox1, oy1, ox2, oy2 = seen[1]
            old_size = max(ox2 - ox1, oy2 - oy1)
            dist = math.hypot(cx - (ox1 + ox2) / 2, cy - (oy1 + oy2) / 2)

            if dist > self.handoff_max_distance * max(new_size, old_size):
                continue

            old_team = self._team_of.get(tid)
            if new_team is None:
                new_team = self._guess_team_id(frame, box)

            if old_team is None or new_team is None or old_team != new_team:
                continue

            found[res["player_id"]] = {
                "player_id": res["player_id"],
                "player_name": res["player_name"],
                "jersey_number": self._number_by_player.get(res["player_id"]),
                "from_track": tid,
            }

        if len(found) != 1:
            return None

        return next(iter(found.values()))

    # ------------------------------------------------------------
    # FACE
    # ------------------------------------------------------------

    def _crop_face_region(self, frame, box):
        """Top of the box for a standing player; the whole box for a player lying down."""

        x1, y1, x2, y2 = [int(v) for v in box]
        box_h, box_w = y2 - y1, x2 - x1

        if box_h <= 0 or box_w <= 0:
            return None

        fh, fw = frame.shape[:2]

        if box_w > box_h:
            # lying down — the head could be at either end
            top, bottom, left, right = y1, y2, x1, x2
        else:
            pad = int(0.10 * box_w)
            top = y1
            bottom = y1 + int(0.30 * box_h)
            left = x1 - pad
            right = x2 + pad

        top, left = max(0, top), max(0, left)
        bottom, right = min(fh, bottom), min(fw, right)

        if bottom <= top or right <= left:
            return None

        crop = frame[top:bottom, left:right]

        # small broadcast faces: enlarge so the face detector can find them
        if crop.shape[0] < 160:
            scale = min(4.0, 160.0 / max(crop.shape[0], 1))
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        return crop

    @staticmethod
    def _cosine_distance(a, b):
        a, b = np.array(a), np.array(b)
        return 1 - (np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    def _try_face(self, track_id, frame, box, frame_index=None):

        if not self.face_ready:
            return None

        if track_id in self._face_gave_up:
            return None

        x1, y1, x2, y2 = [float(v) for v in box]
        box_h, box_w = y2 - y1, x2 - x1
        lying_down = box_w > box_h

        if not lying_down and box_h < self.face_min_box_height:
            return None

        n = self.face_every_n_frames
        if frame_index is not None and n and n > 1 and (frame_index + track_id) % n != 0:
            return None

        from deepface import DeepFace

        crop = self._crop_face_region(frame, box)

        if crop is None or crop.size == 0:
            return None

        self._face_attempts[track_id] = self._face_attempts.get(track_id, 0) + 1
        self._stats["face_attempts"] += 1

        try:
            reps = DeepFace.represent(
                img_path=crop,
                model_name=self.face_model_name,
                detector_backend=self.face_detector,
                enforce_detection=True,     # no face found -> exception -> skip (no junk matches)
            )
        except Exception:
            reps = []

        face = self._largest_face(reps)

        if face is None:
            self._stats["face_not_found"] += 1

        else:

            query = face["embedding"]

            # closest reference face per player
            best_per_player = {}

            for player_id, name, ref in self._face_gallery:
                d = self._cosine_distance(query, ref)
                if player_id not in best_per_player or d < best_per_player[player_id]:
                    best_per_player[player_id] = d

            ranked = sorted((d, pid) for pid, d in best_per_player.items())

            best_dist, best_pid = ranked[0]
            second_dist = ranked[1][0] if len(ranked) > 1 else 1.0

            clear_winner = (
                best_dist <= self.face_distance_threshold
                and (second_dist - best_dist) >= self.face_margin
            )

            if clear_winner:

                # the face must belong to the team the shirt colour says (when the colour is clear)
                shirt_team = self._guess_team_id(frame, box)

                if shirt_team is not None and shirt_team != self._team_by_player.get(best_pid):
                    self._stats["face_rejected_team"] += 1

                else:

                    name = self._name_by_player[best_pid]

                    votes = self._face_votes.setdefault(track_id, {})
                    votes.setdefault(name, []).append(best_dist)

                    print(f"[DEBUG-FACE] track {track_id} looks like {name} (distance {best_dist:.3f})")

                    if len(votes[name]) >= self.face_min_votes:

                        dists = votes[name]

                        return {
                            "player_id": best_pid,
                            "player_name": name,
                            "method": "face",
                            "detail": "face match (" + str(len(dists)) + " frames)",
                            "confidence": round(1 - (sum(dists) / len(dists)), 3),
                            "vote_count": len(dists),
                        }

        if self._face_attempts[track_id] >= self.face_max_attempts:
            self._face_gave_up.add(track_id)

        return None

    # ------------------------------------------------------------
    # CROP HISTORY (read at fall time)
    # ------------------------------------------------------------

    def remember_crop(self, track_id, frame, box, frame_index, keypoints=None, keypoint_conf=None):
        """Keeps a few recent jersey crops per track so a fall can be identified from BEFORE the player went down."""

        if self._box_size(box) < self.min_box_size:
            return

        if frame_index % self.history_every_n_frames != 0:
            return

        self._active_kps = (keypoints, keypoint_conf) if keypoints is not None and keypoint_conf is not None else None

        crop = self._crop_jersey_region(frame, box)

        self._active_kps = None

        if crop is None or crop.size == 0:
            return

        history = self._crop_history.setdefault(track_id, [])

        # .copy() so we keep only the small crop, not the whole 1080p frame
        history.append((frame_index, crop.copy(), self._guess_team_id(frame, box)))

        if len(history) > self.history_max_crops:
            history.pop(0)

        if frame_index % 150 == 0:
            for tid in list(self._crop_history):
                seen = self._last_seen.get(tid)
                if seen is None or frame_index - seen[0] > self.history_forget_frames:
                    self._crop_history.pop(tid, None)

    def identify_from_history(self, track_id):
        """Reads the remembered crops of this track right now. Returns a result dict or None."""

        confirmed = self._confirmed.get(track_id)

        if confirmed is not None:
            return confirmed

        history = self._crop_history.get(track_id)

        if not history:
            return None

        votes = {}

        for _, crop, team_id in history:

            number, conf = self._read_number_from_crop(crop)

            if number is None or conf < self.ocr_confidence_threshold:
                continue

            votes.setdefault((number, team_id), []).append(conf)

        best = None

        numbers_read = {}
        for (number, _), confs in votes.items():
            numbers_read.setdefault(number, []).extend(confs)

        for (number, team_id), confs in votes.items():

            if self._goalkeeper_misread(number, team_id):
                continue

            match = self._match_roster(number, team_id)

            if match is None:
                continue

            avg = sum(confs) / len(confs)

            if self._is_partial_digit(number, team_id):
                if (
                    len(confs) < PARTIAL_DIGIT_MIN_VOTES - 1
                    or avg < PARTIAL_DIGIT_MIN_AVG_CONF
                    or self._has_longer_read(number, numbers_read)
                ):
                    continue

            elif not (max(confs) >= 0.92 or (len(confs) >= 2 and avg >= 0.60)):
                continue

            score = len(confs) * avg

            if best is None or score > best[0]:
                best = (score, match, number, confs, team_id)

        if best is None:
            return None

        _, match, number, confs, team_id = best

        result = {
            "player_id": match[0],
            "player_name": match[1],
            "method": "jersey_history",
            "detail": "jersey #" + str(number) + " (read from " + str(len(history)) + " frames before the event)",
            "confidence": round(sum(confs) / len(confs), 3),
            "vote_count": len(confs),
        }

        ref = self._last_seen.get(track_id)
        ref_frame = ref[0] if ref else None

        if self._player_live_on_other_track(result["player_id"], track_id, ref_frame):
            self._stats["duplicate_blocked"] += 1
            print(f"[ID-GUARD] track {track_id} history read {result['player_name']} but that player "
                  f"is already live on another track - keeping UNKNOWN")
            return None

        self._confirmed[track_id] = result
        self._team_of[track_id] = team_id
        self._log(track_id, result)

        return result

    # ------------------------------------------------------------
    # AUTO FACE GALLERY
    # A player confirmed by a clear jersey read teaches the
    # system their face, so later they can be recognised even
    # when the number is hidden. No photo uploads needed.
    # ------------------------------------------------------------

    def _learn_face(self, track_id, frame, box, frame_index, result):

        if not (self.enable_face and self.auto_face_gallery):
            return

        if result.get("player_id") is None:
            return

        if not str(result.get("method", "")).startswith("jersey"):
            return   # never learn from a face match (would reinforce mistakes)

        if (result.get("confidence") or 0) < self.auto_face_min_confidence:
            return

        player_id = result["player_id"]

        if self._auto_face_count.get(player_id, 0) >= self.auto_face_per_player:
            return

        if self._auto_face_tracks.get(track_id, 0) >= 3:
            return   # spread learned faces across different moments of the match

        n = self.auto_face_every_n_frames
        if frame_index is None or (frame_index + track_id) % n != 0:
            return

        x1, y1, x2, y2 = [float(v) for v in box]

        if (x2 - x1) > (y2 - y1):
            return   # lying down — skip, faces are usually distorted

        if (y2 - y1) < self.face_min_box_height:
            return

        # the shirt colour must agree with the player's team
        shirt_team = self._guess_team_id(frame, box)
        if shirt_team is not None and shirt_team != self._team_by_player.get(player_id):
            return

        crop = self._crop_face_region(frame, box)

        if crop is None or crop.size == 0:
            return

        # how much _crop_face_region enlarged the crop
        original_h = max(1, int(0.30 * (y2 - y1)))
        scale = crop.shape[0] / float(original_h)

        from deepface import DeepFace

        try:
            reps = DeepFace.represent(
                img_path=crop,
                model_name=self.face_model_name,
                detector_backend=self.face_detector,
                enforce_detection=True,
            )
        except Exception:
            return

        face = self._largest_face(reps)

        if face is None:
            return

        fa = face.get("facial_area") or {}
        face_h_original = fa.get("h", 0) / max(scale, 1e-6)

        if face_h_original < self.auto_face_min_face_px:
            return

        name = self._name_by_player[player_id]

        self._face_gallery.append((player_id, name, face["embedding"]))
        self._auto_face_count[player_id] = self._auto_face_count.get(player_id, 0) + 1
        self._auto_face_tracks[track_id] = self._auto_face_tracks.get(track_id, 0) + 1
        self._stats["auto_faces_learned"] += 1

        # save the face so the next run starts with it (and you can check it by eye)
        try:
            fx, fy = int(fa.get("x", 0)), int(fa.get("y", 0))
            fw, fh = int(fa.get("w", 0)), int(fa.get("h", 0))
            pad = int(0.25 * max(fw, fh))
            face_img = crop[max(0, fy - pad):fy + fh + pad, max(0, fx - pad):fx + fw + pad]

            folder = os.path.join(
                self.auto_faces_dir,
                str(self._number_by_player.get(player_id)) + " - " + name
            )
            os.makedirs(folder, exist_ok=True)

            filename = "auto_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jpg"
            cv2.imwrite(os.path.join(folder, filename), face_img)

        except Exception as e:
            print("[PlayerIdentifier] Could not save learned face:", e)

        print(
            f"[AUTO-FACE] learned face #{self._auto_face_count[player_id]} for {name} "
            f"from track {track_id} ({result['detail']})"
        )

    # ------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------

    def identify(self, track_id, frame, box, frame_index=None, keypoints=None, keypoint_conf=None):
        """Call once per frame for each tracked player box. Pass frame_index to enable hand-off tracking,
        and the pose keypoints so the jersey number is cropped from the torso."""

        if frame_index is not None:
            self._touch(track_id, frame_index, box)

        if track_id in self._confirmed:

            confirmed = self._confirmed[track_id]

            try:
                self._learn_face(track_id, frame, box, frame_index, confirmed)
            except Exception as e:
                print("[PlayerIdentifier] auto face error:", e)

            return confirmed

        self._active_kps = (keypoints, keypoint_conf) if keypoints is not None and keypoint_conf is not None else None

        prior = None

        if frame_index is not None:

            self._maybe_reopen(track_id, box)

            prior = self._handoff_prior.get(track_id)

            if (
                prior is None
                and self.handoff_enabled
                and frame_index - self._first_seen[track_id] <= self.handoff_max_gap_frames
            ):
                prior = self._find_handoff_candidate(track_id, frame, box, frame_index)

                if prior is not None:
                    self._handoff_prior[track_id] = prior
                    self._stats["handoff_candidates"] += 1

        result = None

        if self._worth_reading(track_id, box, frame_index):

            result = self._try_jersey(track_id, frame, box, prior)

            if result is None and self._box_size(box) >= self.name_min_box_size:
                result = self._try_jersey_name(track_id, frame, box, prior)

        if result is None:
            result = self._try_face(track_id, frame, box, frame_index)

        if result is not None and self._player_live_on_other_track(result.get("player_id"), track_id, frame_index):
            # the same player cannot be on two live tracks at once
            self._stats["duplicate_blocked"] += 1
            print(f"[ID-GUARD] track {track_id} read {result.get('player_name')} but that player is "
                  f"already live on another track - keeping UNKNOWN")
            result = None

        if result is not None:

            self._confirmed[track_id] = result

            if frame_index is not None:
                self._team_of[track_id] = self._guess_team_id(frame, box)
                self._confirm_frames.append(frame_index - self._first_seen[track_id])

            if result["method"] == "jersey_handoff":
                self._stats["handoff_confirmed"] += 1

            self._log(track_id, result)

        self._active_kps = None

        return result

    def force_allow_retry(self, track_id):
        """Removes a track from the give-up lists so identify() keeps trying."""

        self._jersey_gave_up.discard(track_id)
        self._jersey_name_gave_up.discard(track_id)
        self._face_gave_up.discard(track_id)

    def get_confirmed(self, track_id):
        """Non-blocking lookup: the identification for this track, or None."""

        return self._confirmed.get(track_id)

    def has_given_up(self, track_id):
        """True if this track used up its attempts on jersey OCR and face."""

        jersey_done = track_id in self._jersey_gave_up
        face_done = (not self.enable_face) or track_id in self._face_gave_up

        return jersey_done and face_done

    def get_report(self):

        seen = len(self._first_seen)

        named = [r for r in self._confirmed.values() if r["player_id"] is not None]
        identified_seen = sum(1 for t in self._confirmed if t in self._first_seen)

        lifetimes = [
            self._last_seen[t][0] - self._first_seen[t] + 1
            for t in self._first_seen if t in self._last_seen
        ]

        return {
            "tracks_seen": seen,
            "tracks_identified": identified_seen,
            "by_method": dict(Counter(r["method"] for r in named)),
            "median_frames_to_identify": statistics.median(self._confirm_frames) if self._confirm_frames else None,
            "tracks_under_30_frames": sum(1 for l in lifetimes if l < 30),
            "median_track_frames": statistics.median(lifetimes) if lifetimes else None,
            **self._stats,
        }

    def print_report(self):

        r = self.get_report()

        seen = r["tracks_seen"]
        pct = (100.0 * r["tracks_identified"] / seen) if seen else 0.0

        print()
        print("====================================")
        print(" IDENTIFICATION REPORT")
        print("====================================")
        print(f" Tracks seen:              {seen}")
        print(f" Tracks identified:        {r['tracks_identified']} ({pct:.0f}%)  by method: {r['by_method']}")
        print(f" Median frames to identify: {r['median_frames_to_identify']}")
        print(f" Median track lifetime:    {r['median_track_frames']} frames")
        print(f" Tracks alive < 30 frames: {r['tracks_under_30_frames']}")
        print(f" OCR skipped, box too small: {r['skipped_small']}   off-cadence: {r['skipped_cadence']}")
        print(f" Given-up tracks reopened: {r['reopened']}")
        print(f" Hand-off: candidates {r['handoff_candidates']}, confirmed {r['handoff_confirmed']}")
        print(f" Reads matching no roster player (ignored): {r['unmatched_reads']}")
        print(f" Goalkeeper numbers on outfield shirts (ignored): {r['rejected_partial']}")
        print(f" Jersey crops: from pose (torso) {r['pose_crops']}, from box (fallback) {r['box_crops']}")
        print(f" Face: {'ON' if self.face_ready else 'OFF'}  attempts {r['face_attempts']}, "
              f"no face found {r['face_not_found']}, rejected by shirt colour {r['face_rejected_team']}")
        print(f" Faces learned automatically this run: {r['auto_faces_learned']} "
              f"(saved in known_players_auto)")
        print("====================================")