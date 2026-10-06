import unittest
from types import SimpleNamespace

from video_processing import _classify_collision_risk, _classify_fall_risk


class FallRiskTests(unittest.TestCase):
    def test_head_region_alone_does_not_make_a_fall_critical(self):
        self.assertEqual(_classify_fall_risk(0.85, "HEAD", 0.95, True, 2, 0.2), ("HIGH", 0.85))

    def test_corroborated_head_impact_is_critical(self):
        self.assertEqual(_classify_fall_risk(0.65, "HEAD", 0.9, True, 2, 0.45), ("CRITICAL", 0.9))

    def test_head_motion_without_a_preceding_standing_posture_is_not_critical(self):
        self.assertEqual(_classify_fall_risk(0.85, "HEAD", 0.9, False, 3, 0.6), ("HIGH", 0.85))

    def test_non_cranial_region_does_not_escalate_to_critical(self):
        self.assertEqual(_classify_fall_risk(0.85, "LOWER LEG", 0.9, True, 6, 0.8), ("HIGH", 0.85))

    def test_existing_risk_thresholds_are_preserved_without_critical_evidence(self):
        self.assertEqual(_classify_fall_risk(0.5, None, 0.7, False, 1, None), ("MEDIUM", 0.5))


class CollisionRiskTests(unittest.TestCase):
    def test_contact_without_fall_evidence_stays_low_even_with_large_overlap(self):
        risk, score = _classify_collision_risk(0.95, 8, [])
        self.assertEqual(risk, "LOW")
        self.assertLess(score, 0.5)

    def test_collision_inherits_confirmed_fall_severity(self):
        fall = SimpleNamespace(risk="HIGH", risk_score=0.82)
        self.assertEqual(_classify_collision_risk(0.8, 3, [fall]), ("HIGH", 0.82))

    def test_collision_is_critical_only_when_associated_fall_is_critical(self):
        fall = SimpleNamespace(risk="CRITICAL", risk_score=0.95)
        self.assertEqual(_classify_collision_risk(0.9, 4, [fall]), ("CRITICAL", 0.95))

    def test_unknown_fall_severity_does_not_escalate_contact(self):
        fall = SimpleNamespace(risk="UNRATED", risk_score=0.99)
        risk, score = _classify_collision_risk(0.9, 8, [fall])
        self.assertEqual(risk, "LOW")
        self.assertLess(score, 0.5)


if __name__ == "__main__":
    unittest.main()
