import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from models import Player
from player_identifier import PlayerIdentifier, TEAM_KIT_COLORS_BGR


class RosterIdentityTests(unittest.TestCase):
    def setUp(self):
        self.identifier = object.__new__(PlayerIdentifier)
        self.identifier.match_team_ids = {1, 2}
        self.identifier.roster_by_jersey_multi = {
            11: [
                (101, "Team A Player 11", 1),
                (201, "Team B Player 11", 2),
            ]
        }

    def test_same_jersey_resolves_with_team_a_evidence(self):
        self.assertEqual(self.identifier._match_roster(11, 1), (101, "Team A Player 11"))

    def test_same_jersey_resolves_with_team_b_evidence(self):
        self.assertEqual(self.identifier._match_roster(11, 2), (201, "Team B Player 11"))

    def test_ambiguous_team_keeps_identity_unknown(self):
        self.assertIsNone(self.identifier._match_roster(11, None))

    def test_inactive_player_is_not_loaded_or_selected(self):
        active = SimpleNamespace(id=101, name="Team A Player 11", team_id=1, jersey_number=11)
        teams = [SimpleNamespace(id=1, name="Team A"), SimpleNamespace(id=2, name="Team B")]
        players_query = MagicMock()
        players_query.filter.return_value.all.return_value = [active]
        teams_query = MagicMock()
        teams_query.all.return_value = teams
        db = MagicMock()
        db.query.side_effect = [players_query, teams_query]

        with patch("player_identifier.SessionLocal", return_value=db):
            self.identifier._load_roster()

        players_query.filter.assert_called_once()
        self.assertTrue(
            players_query.filter.call_args.args[0].compare(Player.active.is_(True))
        )
        self.assertEqual(self.identifier._match_roster(11, 1), (101, "Team A Player 11"))
        self.assertIsNone(self.identifier._match_roster(11, 2))

    def test_portugal_kit_color_maps_only_to_portugal_roster(self):
        self.identifier.teams_by_id = {1: "Portugal", 2: "Netherlands"}
        self.identifier._roster_team_ids = {1, 2}
        self.identifier._color_debug_count = 30
        frame = np.empty((100, 100, 3), dtype=np.uint8)
        frame[:] = TEAM_KIT_COLORS_BGR["Portugal"]

        self.assertEqual(self.identifier._guess_team_id(frame, [0, 0, 100, 100]), 1)

    def test_netherlands_orange_kit_color_maps_only_to_netherlands_roster(self):
        self.identifier.teams_by_id = {1: "Portugal", 2: "Netherlands"}
        self.identifier._roster_team_ids = {1, 2}
        self.identifier._color_debug_count = 30
        frame = np.empty((100, 100, 3), dtype=np.uint8)
        frame[:] = TEAM_KIT_COLORS_BGR["Netherlands"]

        self.assertEqual(self.identifier._guess_team_id(frame, [0, 0, 100, 100]), 2)

    def test_uncertain_kit_color_does_not_select_a_team(self):
        self.identifier.teams_by_id = {1: "Portugal", 2: "Netherlands"}
        self.identifier._roster_team_ids = {1, 2}
        self.identifier._color_debug_count = 30
        portugal = np.array(TEAM_KIT_COLORS_BGR["Portugal"], dtype=np.float32)
        netherlands = np.array(TEAM_KIT_COLORS_BGR["Netherlands"], dtype=np.float32)
        frame = np.empty((100, 100, 3), dtype=np.uint8)
        frame[:] = ((portugal + netherlands) / 2).astype(np.uint8)

        self.assertIsNone(self.identifier._guess_team_id(frame, [0, 0, 100, 100]))


if __name__ == "__main__":
    unittest.main()
