r"""
Checks the face photo folders before a run.

Usage (from the src folder):
    python check_face_gallery.py            -> report which players have usable face photos
    python check_face_gallery.py --create   -> also create an empty folder for every roster player

Folder layout:
    <project root>\known_players\17 - Cristiano Ronaldo\1.jpg
    <project root>\known_players\17 - Cristiano Ronaldo\2.jpg
"""

import os
import sys

from database import SessionLocal
from models import Player, Team
from player_identifier import plain_text, resolve_player_folder, IMAGE_EXTENSIONS

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWN_PLAYERS_DIR = os.path.join(PROJECT_ROOT, "known_players")

FACE_MODEL = "Facenet512"
FACE_DETECTOR = "opencv"     # keep the same as face_detector in player_identifier.py


def main():

    create = "--create" in sys.argv

    db = SessionLocal()
    players = db.query(Player).all()
    teams = {t.id: t.name for t in db.query(Team).all()}
    db.close()

    roster_by_plain_name = {plain_text(p.name): p.name for p in players}

    os.makedirs(KNOWN_PLAYERS_DIR, exist_ok=True)

    if create:
        for p in players:
            folder = os.path.join(KNOWN_PLAYERS_DIR, str(p.jersey_number) + " - " + p.name)
            if resolve_player_folder_exists(p.name, roster_by_plain_name) is None:
                os.makedirs(folder, exist_ok=True)
        print("Created folders in", KNOWN_PLAYERS_DIR)
        print()

    from deepface import DeepFace

    usable = {}
    problems = []

    for folder in sorted(os.listdir(KNOWN_PLAYERS_DIR)):

        folder_path = os.path.join(KNOWN_PLAYERS_DIR, folder)

        if not os.path.isdir(folder_path):
            continue

        name = resolve_player_folder(folder, roster_by_plain_name)

        if name is None:
            problems.append("Folder does not match any player in the database: " + folder)
            continue

        for filename in sorted(os.listdir(folder_path)):

            if not filename.lower().endswith(IMAGE_EXTENSIONS):
                continue

            path = os.path.join(folder_path, filename)

            try:
                DeepFace.represent(
                    img_path=path,
                    model_name=FACE_MODEL,
                    detector_backend=FACE_DETECTOR,
                    enforce_detection=True,
                )
                usable[name] = usable.get(name, 0) + 1

            except Exception:
                problems.append("No face found in: " + os.path.join(folder, filename))

    print("====================================")
    print(" FACE GALLERY CHECK")
    print("====================================")

    for team_id, team_name in sorted(teams.items()):

        print()
        print(team_name)

        for p in sorted((p for p in players if p.team_id == team_id), key=lambda p: p.jersey_number):

            count = usable.get(p.name, 0)
            status = "OK " if count >= 3 else ("LOW" if count > 0 else "---")
            print("  [" + status + "] #" + str(p.jersey_number).ljust(3), p.name.ljust(28), count, "photo(s)")

    if problems:
        print()
        print("Problems:")
        for line in problems:
            print("  -", line)

    print()
    print("Players with usable photos:", len(usable), "of", len(players))
    print("Aim for 3-5 clear, front-facing photos per player (OK = 3 or more).")
    print("====================================")


def resolve_player_folder_exists(player_name, roster_by_plain_name):
    """Returns an existing folder for this player, or None."""

    for folder in os.listdir(KNOWN_PLAYERS_DIR):
        if os.path.isdir(os.path.join(KNOWN_PLAYERS_DIR, folder)):
            if resolve_player_folder(folder, roster_by_plain_name) == player_name:
                return folder

    return None


if __name__ == "__main__":
    main()