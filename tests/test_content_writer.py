"""Tests for the Welcome content writer agent — guards, selection, facts."""

import json

import pytest

from src.writer.content_writer import (ContentWriter, WriterOutput,
                                       build_fact_sheet, load_templates,
                                       preserve_sentences, run_guards,
                                       select_template)

BRAND_CFG = json.load(open("config/brand_defaults.json", encoding="utf-8"))

SCHOOL = {
    "school_id": "7440", "brand": "La Petite Academy",
    "school_name": "La Petite Academy on Beach Blvd",
    "street_address": "7801 Beach Blvd", "city": "Jacksonville", "state": "FL",
    "url": "https://www.lapetite.com/your-local-school/jacksonville-fl-7440/",
}

CURRENT = ("Welcome to La Petite Academy on Beach Boulevard in Jacksonville, "
           "Florida! Since 1991, we have provided quality daycare and early "
           "education in a safe, supportive environment. We are conveniently "
           "located on Beach Boulevard between Hodges Boulevard and San Pablo "
           "Road, across from Denny's. Schedule a tour today!")


def obs(params=None):
    base = {
        "GEO_06": {"parameter_name": "Landmark 1", "welcome_status": "PRESENT",
                   "page_status": "PRESENT",
                   "welcome_evidence": "across from Denny's", "page_evidence": None},
        "LEAD_03": {"parameter_name": "Years Serving", "welcome_status": "PRESENT",
                    "page_status": "PRESENT",
                    "welcome_evidence": "Since 1991", "page_evidence": None},
        "LEAD_01": {"parameter_name": "Director Name", "welcome_status": "ABSENT",
                    "page_status": "PRESENT", "welcome_evidence": None,
                    "page_evidence": "Dee Pryor, Director"},
        "QUAL_03": {"parameter_name": "Awards", "welcome_status": "ABSENT",
                    "page_status": "ABSENT", "welcome_evidence": None,
                    "page_evidence": None},
    }
    if params:
        base.update(params)
    return {"parameters": base}


class TestTemplates:
    def test_load_all_25(self):
        t = load_templates("La Petite Academy")
        assert len(t) == 25
        assert t[0]["id"] == "Sample 1"
        assert "City, State" in t[0]["placeholders"]

    def test_unknown_brand_empty(self):
        assert load_templates("Childtime") == []

    def test_selection_deterministic_rotation(self):
        t = load_templates("La Petite Academy")
        facts = {"City, State": "Jacksonville, FL", "City": "Jacksonville"}
        a = select_template(t, facts, "7440")
        b = select_template(t, facts, "7440")
        assert a["id"] == b["id"]  # deterministic
        c = select_template(t, facts, "7441")
        assert c is not None       # neighbor may rotate but always valid


class TestFactSheet:
    def test_only_verified_facts_enter(self):
        facts = build_fact_sheet(SCHOOL, obs(), BRAND_CFG, None)
        assert facts["City, State"] == "Jacksonville, FL"
        assert "Denny's" in facts["Landmark 1"]            # page-quoted
        assert "Dee Pryor" in facts["Director Name"]       # page-quoted
        assert "Since 1991" in facts["X years serving community"]
        assert "SproutAbout" in facts["Technology/Safety Feature"]
        assert "Landmark 2" not in facts                   # nothing verified
        assert "Facility Highlight/Recognition" not in facts  # QUAL_03 absent

    def test_unapproved_register_locality_never_enters(self):
        row = {"locality": "Golden Glades/The Woods", "approved": ""}
        facts = build_fact_sheet(SCHOOL, obs(), BRAND_CFG, row)
        assert all("Golden Glades" not in str(v) for v in facts.values())
        approved = dict(row, approved="yes")
        facts2 = build_fact_sheet(SCHOOL, obs(), BRAND_CFG, approved)
        assert any("Golden Glades" in str(v) for v in facts2.values())


class TestPreserve:
    def test_healthy_sentences_selected(self):
        keep = preserve_sentences(CURRENT, obs())
        joined = " ".join(keep)
        assert "across from Denny's" in joined
        assert "Since 1991" in joined
        assert all("Schedule a tour" not in s for s in keep)  # no PRESENT evidence


class TestGuards:
    ALLOWED = CURRENT + " SproutAbout La Petite Academy Jacksonville Florida FL"

    def test_clean_draft_passes(self):
        copy = (CURRENT + " Our early education and child care programs include "
                "preschool and daycare options. [CLIENT INPUT NEEDED: Landmark 2] "
                + "children learn and grow with caring teachers every day " * 10)
        flags = run_guards(copy, ["Since 1991, we have provided quality daycare "
                                  "and early education in a safe, supportive "
                                  "environment."], self.ALLOWED)
        assert flags == []

    def test_lost_preserved_sentence_flagged(self):
        copy = ("A fully rewritten welcome about daycare, preschool, child care "
                "and early education in Jacksonville. " + "word " * 140)
        flags = run_guards(copy, ["We are conveniently located on Beach Boulevard "
                                  "between Hodges Boulevard and San Pablo Road, "
                                  "across from Denny's."], self.ALLOWED)
        assert any(f.startswith("PRESERVE_LOST") for f in flags)

    def test_invented_entity_flagged(self):
        copy = (CURRENT + " We sit right beside Willowbrook Shopping Plaza. "
                "daycare preschool child care early education")
        flags = run_guards(copy, [], self.ALLOWED)
        assert any("NEW_ENTITY:Willowbrook Shopping Plaza" in f for f in flags)

    def test_missing_head_keyword_flagged(self):
        copy = ("Welcome to our lovely school in Jacksonville where children "
                "thrive. " + "nice words here " * 50)
        flags = run_guards(copy, [], self.ALLOWED)
        assert any("MISSING_HEAD_KEYWORD" in f for f in flags)


class FakeWriterClient:
    def __init__(self, copy_text):
        self.copy_text = copy_text
        self.total_cost_usd = 0.0

    def evaluate(self, system_prompt, payload, schema):
        from src.evaluator.llm_client import LLMCallMeta
        return (WriterOutput(final_copy=self.copy_text,
                             unfilled_placeholders=["Landmark 2"],
                             preserved_kept=[], gap_notes=["covered X"]),
                LLMCallMeta(model="fake-writer"))


class TestAgent:
    def test_draft_school_end_to_end_with_fake_llm(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.writer.content_writer.DRAFTS_DIR", tmp_path)
        good_copy = (CURRENT + " Our early education and child care programs "
                     "include preschool and daycare for every age. "
                     "[CLIENT INPUT NEEDED: Landmark 2] "
                     + "children learn and grow with caring teachers every day " * 10)
        w = ContentWriter(BRAND_CFG, llm_client=FakeWriterClient(good_copy))
        d = w.draft_school(SCHOOL, obs(), CURRENT, None)
        assert d["status"] == "CLEAN"
        assert d["template_used"].startswith("Sample")
        assert "Landmark 2" in d["client_inputs_needed"]
        assert (tmp_path / "7440_draft.json").exists()

    def test_no_templates_brand(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.writer.content_writer.DRAFTS_DIR", tmp_path)
        w = ContentWriter(BRAND_CFG, llm_client=FakeWriterClient("x"))
        d = w.draft_school(dict(SCHOOL, brand="Childtime"), obs(), CURRENT, None)
        assert d["status"] == "NO_TEMPLATES"
