"""S6 unit tests — semantic evaluator v2 with a mocked LLM client.

The hallucination guard, landmark guard, upgrade-only merge, and failure
containment are all Python-side logic and fully testable without API access.
"""

import json

import pytest

from src.evaluator.llm_client import LLMCallMeta, LLMUnavailable
from src.evaluator.semantic_evaluator import (
    SEMANTIC_PARAM_IDS, ParamEval, ScopeEval, SemanticEvaluator, SemanticOutput,
    evidence_in_text,
)

PARAMS = json.load(open("config/parameters_database.json", encoding="utf-8"))["parameters"]
BRAND_CFG = json.load(open("config/brand_defaults.json", encoding="utf-8"))

SCHOOL = {
    "school_id": "7440", "brand": "La Petite Academy",
    "school_name": "La Petite Academy on Beach Blvd",
    "street_address": "7801 Beach Blvd", "city": "Jacksonville", "state": "FL",
}
WELCOME = ("Welcome to La Petite Academy on Beach Boulevard in Jacksonville, "
           "Florida! We are conveniently located on Beach Boulevard between "
           "Hodges Boulevard and San Pablo Road, across from Denny’s. "
           "Our infant care program nurtures every child.")
REST = ("Our programs include Infants, Toddlers and Pre-K. Dee Pryor, Director, "
        "leads our team. Schedule a tour today!")


def extracted():
    return {"school_id": "7440", "welcome_found": True, "welcome_text": WELCOME,
            "welcome_word_count": len(WELCOME.split()), "body_rest_text": REST}


class FakeClient:
    """Returns a pre-programmed SemanticOutput (or raises)."""

    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.last_payload = None

    def evaluate(self, system_prompt, user_payload, schema):
        self.last_payload = user_payload
        if self.error:
            raise self.error
        return self.output, LLMCallMeta(model="fake-model")


def scope(status, evidence=None, conf=0.9):
    return ScopeEval(status=status, evidence=evidence, confidence=conf)


def absent():
    return scope("ABSENT", None, 0.9)


def full_output(**overrides):
    """All semantic params ABSENT except overrides {pid: ParamEval}."""
    evals = []
    for pid in SEMANTIC_PARAM_IDS:
        if pid in overrides:
            evals.append(overrides[pid])
        else:
            evals.append(ParamEval(param_id=pid, welcome=absent(), rest=absent()))
    return SemanticOutput(evaluations=evals)


def make_eval(client):
    return SemanticEvaluator(PARAMS, BRAND_CFG, llm_client=client)


class TestEvidenceGuard:
    def test_verified_evidence_survives(self):
        out = full_output(GEO_06=ParamEval(
            param_id="GEO_06",
            welcome=scope("PRESENT", "across from Denny's"),  # straight quote
            rest=absent()))
        res, meta = make_eval(FakeClient(out)).evaluate(extracted(), SCHOOL, {})
        # page text has the CURLY quote; the normalized check must still verify
        assert res["GEO_06"]["welcome_status"] == "PRESENT"
        assert res["GEO_06"]["flags"] == []

    def test_hallucinated_evidence_downgraded(self):
        out = full_output(QUAL_01=ParamEval(
            param_id="QUAL_01",
            welcome=scope("PRESENT", "accredited by NECPA with Gold Seal status"),
            rest=absent()))
        res, _ = make_eval(FakeClient(out)).evaluate(extracted(), SCHOOL, {})
        assert res["QUAL_01"]["welcome_status"] == "ABSENT"
        assert "W:EVIDENCE_UNVERIFIED" in res["QUAL_01"]["flags"]
        assert res["QUAL_01"]["welcome_evidence"] is None

    def test_evidence_from_wrong_scope_downgraded(self):
        # "Dee Pryor, Director" exists only in REST; claiming it for welcome fails
        out = full_output(LEAD_01=ParamEval(
            param_id="LEAD_01",
            welcome=scope("PRESENT", "Dee Pryor, Director"),
            rest=scope("PRESENT", "Dee Pryor, Director")))
        res, _ = make_eval(FakeClient(out)).evaluate(extracted(), SCHOOL, {})
        assert res["LEAD_01"]["welcome_status"] == "ABSENT"
        assert res["LEAD_01"]["rest_status"] == "PRESENT"

    def test_landmark_generic_geo_downgraded(self):
        out = full_output(GEO_06=ParamEval(
            param_id="GEO_06",
            welcome=scope("PRESENT", "in Jacksonville, Florida"),
            rest=absent()))
        res, _ = make_eval(FakeClient(out)).evaluate(extracted(), SCHOOL, {})
        assert res["GEO_06"]["welcome_status"] == "ABSENT"
        assert "W:LANDMARK_GENERIC_GEO" in res["GEO_06"]["flags"]

    def test_real_landmark_with_city_in_quote_survives(self):
        out = full_output(GEO_06=ParamEval(
            param_id="GEO_06",
            welcome=scope("PRESENT",
                          "located on Beach Boulevard between Hodges Boulevard "
                          "and San Pablo Road, across from Denny's"),
            rest=absent()))
        res, _ = make_eval(FakeClient(out)).evaluate(extracted(), SCHOOL, {})
        assert res["GEO_06"]["welcome_status"] == "PRESENT"


class TestKeywordUpgrade:
    def det(self, w="ABSENT", r="ABSENT"):
        return {"KW_01": {"welcome_status": w, "rest_status": r,
                          "page_status": "ABSENT" if w == r == "ABSENT" else "PRESENT",
                          "welcome_evidence": None, "page_evidence": None,
                          "flags": [], "counts": {}}}

    def test_verified_paraphrase_upgrades_to_partial(self):
        out = full_output()
        out.keyword_paraphrases = [ParamEval(
            param_id="KW_01",
            welcome=scope("PRESENT", "infant care program"),
            rest=absent())]
        det = self.det()
        make_eval(FakeClient(out)).evaluate(extracted(), SCHOOL, det)
        assert det["KW_01"]["welcome_status"] == "PARTIAL"  # never PRESENT
        assert det["KW_01"]["page_status"] == "PARTIAL"
        assert any("SEMANTIC_PARAPHRASE_UPGRADE" in f for f in det["KW_01"]["flags"])

    def test_deterministic_present_never_touched(self):
        out = full_output()
        out.keyword_paraphrases = [ParamEval(
            param_id="KW_01",
            welcome=scope("ABSENT"),  # LLM disagrees with regex — ignored
            rest=absent())]
        det = self.det(w="PRESENT", r="PRESENT")
        make_eval(FakeClient(out)).evaluate(extracted(), SCHOOL, det)
        assert det["KW_01"]["welcome_status"] == "PRESENT"

    def test_unverified_paraphrase_not_upgraded(self):
        out = full_output()
        out.keyword_paraphrases = [ParamEval(
            param_id="KW_01",
            welcome=scope("PRESENT", "all-day childminding services"),  # not in text
            rest=absent())]
        det = self.det()
        make_eval(FakeClient(out)).evaluate(extracted(), SCHOOL, det)
        assert det["KW_01"]["welcome_status"] == "ABSENT"


class TestFailureContainment:
    def test_llm_unavailable_yields_na_not_absent(self):
        client = FakeClient(error=LLMUnavailable("no credentials"))
        res, meta = make_eval(client).evaluate(extracted(), SCHOOL, {})
        assert meta is None
        assert all(res[pid]["welcome_status"] == "N_A" for pid in SEMANTIC_PARAM_IDS)
        assert all("SEMANTIC_FAILED" in res[pid]["flags"] for pid in SEMANTIC_PARAM_IDS)


class TestPayloadIsolation:
    def test_payload_contains_tier0_but_no_register_names(self):
        client = FakeClient(full_output())
        make_eval(client).evaluate(extracted(), SCHOOL, {})
        p = client.last_payload
        assert "Jacksonville" in p and "7801 Beach Blvd" in p
        # Tier-2 register locality for 7440 must never be shown to the LLM
        assert "Golden Glades" not in p


def test_evidence_in_text_normalization():
    assert evidence_in_text("across from Denny's", "right across from Denny’s today")
    assert evidence_in_text("ACROSS   FROM denny's", "across from Denny’s")
    assert not evidence_in_text("near Willow Creek Park", "across from Denny's")
    assert not evidence_in_text(None, "anything")
