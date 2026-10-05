"""S7 unit tests — scoring engine v2 vs hand-calculated fixtures."""

import json

import pytest

from src.scoring.scoring_engine import ScoringEngine, location_state

PARAMS = json.load(open("config/parameters_database.json", encoding="utf-8"))["parameters"]
TOTAL_WEIGHT = sum(float(p["weight"]) for p in PARAMS)
MH_WEIGHT = sum(float(p["weight"]) for p in PARAMS if p.get("must_have"))
GEO_04_W = next(float(p["weight"]) for p in PARAMS if p["id"] == "GEO_04")
STR_WEIGHT = sum(float(p["weight"]) for p in PARAMS if p["id"] in ("STR_01", "STR_02"))


def res(w="ABSENT", r="ABSENT", p=None, conf=None, flags=None):
    if p is None:
        order = {"PRESENT": 3, "PARTIAL": 2, "ABSENT": 1, "N_A": 0}
        p = max((s for s in (w, r) if s != "N_A"), key=lambda s: order[s], default="N_A")
    return {"welcome_status": w, "rest_status": r, "page_status": p,
            "welcome_evidence": None, "page_evidence": None, "source_tier": "T",
            "provenance": "test", "flags": flags or [], "counts": {},
            "method": "TEST", "confidence": conf}


def all_absent():
    return {p["id"]: res() for p in PARAMS}


def extracted():
    return {"welcome_found": True, "welcome_word_count": 214,
            "extraction_method": "BRAND_SELECTOR", "extractor_version": "2.0.0"}


@pytest.fixture
def engine(tmp_path):
    return ScoringEngine(PARAMS, observations_dir=tmp_path, config_hash="testhash")


class TestLocationState:
    def test_matrix(self):
        assert location_state("PRESENT", "PRESENT") == ("BOTH", "OPTIMAL")
        assert location_state("PRESENT", "ABSENT") == ("WELCOME_ONLY", "KEEP_REINFORCE")
        assert location_state("ABSENT", "PRESENT") == ("WHOLE_PAGE_ONLY", "MOVE_OR_SURFACE")
        assert location_state("ABSENT", "ABSENT") == ("ABSENT", "CREATE_NEW_CONTENT")
        assert location_state("PARTIAL", "ABSENT") == ("WELCOME_ONLY", "KEEP_REINFORCE")
        assert location_state("N_A", "N_A") == ("N_A", "NOT_APPLICABLE")
        assert location_state("PRESENT", "N_A") == ("STRUCTURAL", "OPTIMAL")
        assert location_state("ABSENT", "N_A") == ("STRUCTURAL", "IMPROVE_STRUCTURE")


class TestScoreMath:
    def test_all_absent_is_zero(self, engine):
        sc = engine.compute_school_scorecard("1", extracted(), all_absent())
        assert sc["s_welcome"] == 0.0 and sc["s_page"] == 0.0 and sc["s_gap"] == 0.0
        assert sc["priority_band"].startswith("Tier 3")

    def test_single_present_hand_calculated(self, engine):
        results = all_absent()
        results["GEO_02"] = res(w="PRESENT", r="PRESENT")  # weight 4.0 (must-have, D1)
        sc = engine.compute_school_scorecard("1", extracted(), results)
        # welcome denominator = all 56 params (none N_A)
        assert sc["s_welcome"] == round(4.0 / TOTAL_WEIGHT * 100, 2)
        # page denominator excludes STR_01/STR_02 only when they are N_A —
        # here the fixture marks every param with page != N_A, so full weight
        assert sc["s_page"] == round(4.0 / TOTAL_WEIGHT * 100, 2)
        assert sc["s_must_have"] == round(4.0 / MH_WEIGHT * 100, 2)

    def test_partial_is_half(self, engine):
        results = all_absent()
        results["GEO_02"] = res(w="PARTIAL")
        sc = engine.compute_school_scorecard("1", extracted(), results)
        assert sc["s_welcome"] == round(2.0 / TOTAL_WEIGHT * 100, 2)

    def test_na_excluded_from_both_sides(self, engine):
        base = all_absent()
        base["GEO_02"] = res(w="PRESENT", r="PRESENT")
        sc_full = engine.compute_school_scorecard("1", extracted(), base)

        na_case = {k: dict(v) for k, v in base.items()}
        na_case["GEO_04"] = res(w="N_A", r="N_A", p="N_A")
        sc_na = engine.compute_school_scorecard("2", extracted(), na_case)
        # removing GEO_04 weight from the denominator must RAISE the score
        assert sc_na["s_welcome"] == round(4.0 / (TOTAL_WEIGHT - GEO_04_W) * 100, 2)
        assert sc_na["s_welcome"] > sc_full["s_welcome"]

    def test_str_page_na_shrinks_page_denominator(self, engine):
        results = all_absent()
        results["STR_01"] = res(w="PRESENT", r="N_A", p="N_A")
        results["STR_02"] = res(w="PRESENT", r="N_A", p="N_A")
        results["GEO_02"] = res(w="PRESENT", r="PRESENT")
        sc = engine.compute_school_scorecard("1", extracted(), results)
        # welcome: STR params still count (PRESENT) alongside GEO_02
        expected_welcome = round((4.0 + STR_WEIGHT) / TOTAL_WEIGHT * 100, 2)
        assert sc["s_welcome"] == expected_welcome
        # page: STR weights leave the denominator entirely
        assert sc["s_page"] == round(4.0 / (TOTAL_WEIGHT - STR_WEIGHT) * 100, 2)

    def test_gap(self, engine):
        results = all_absent()
        results["GEO_02"] = res(w="ABSENT", r="PRESENT", p="PRESENT")
        sc = engine.compute_school_scorecard("1", extracted(), results)
        assert sc["s_welcome"] == 0.0
        assert sc["s_page"] > 0
        assert sc["s_gap"] == sc["s_page"]
        assert sc["parameters"]["GEO_02"]["location_state"] == "WHOLE_PAGE_ONLY"

    def test_confidence_mean_excludes_unverified(self, engine):
        results = all_absent()
        results["GEO_06"] = res(w="PRESENT", r="ABSENT", conf=0.9)
        results["QUAL_01"] = res(w="ABSENT", r="ABSENT", conf=0.1,
                                 flags=["W:EVIDENCE_UNVERIFIED"])
        sc = engine.compute_school_scorecard("1", extracted(), results)
        assert sc["mean_confidence"] == 0.9


class TestCaching:
    def test_hash_mismatch_invalidates(self, tmp_path):
        e1 = ScoringEngine(PARAMS, observations_dir=tmp_path, config_hash="hashA")
        e1.compute_school_scorecard("42", extracted(), all_absent())
        assert e1.is_observed("42")
        e2 = ScoringEngine(PARAMS, observations_dir=tmp_path, config_hash="hashB")
        assert not e2.is_observed("42")

    def test_envelope_recorded(self, tmp_path):
        e = ScoringEngine(PARAMS, observations_dir=tmp_path, config_hash="h")
        sc = e.compute_school_scorecard("42", extracted(), all_absent(),
                                        llm_meta={"model": "fake"})
        assert sc["envelope"]["config_hash"] == "h"
        assert sc["envelope"]["llm"]["model"] == "fake"
        saved = json.loads(e.observation_path("42").read_text(encoding="utf-8"))
        assert saved["envelope"]["engine_version"] == "2.0.0"
