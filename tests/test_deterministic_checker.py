"""S5 unit tests — deterministic checker v2 (synthetic snippet suite)."""

import csv
import json

import pytest

from src.evaluator.deterministic_checker import DeterministicChecker, combine_page_status
from src.matching.geo_matcher import LocalityResolver

PARAMS = json.load(open("config/parameters_database.json", encoding="utf-8"))["parameters"]
BRAND_CFG = json.load(open("config/brand_defaults.json", encoding="utf-8"))

SCHOOL = {
    "school_id": "7440", "brand": "La Petite Academy",
    "school_name": "La Petite Academy on Beach Blvd",
    "street_address": "7801 Beach Blvd", "city": "Jacksonville", "state": "FL",
}


def extracted(welcome="", rest="", found=None):
    if found is None:
        found = bool(welcome)
    return {
        "school_id": "7440", "welcome_found": found,
        "welcome_text": welcome, "welcome_word_count": len(welcome.split()),
        "body_text": (welcome + " " + rest).strip(), "body_rest_text": rest,
    }


@pytest.fixture
def resolver(tmp_path):
    cols = ["school_id", "locality", "locality_aliases", "rio_city", "state",
            "relationship", "distance_mi", "source_run_date", "source_workbook",
            "seo_flags", "auto_review_flags", "snapshot_hash",
            "approved", "approved_by", "approved_date", "analyst_notes"]
    with open(tmp_path / "verified_locality_register.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerow(dict.fromkeys(cols, "") | {
            "school_id": "5001", "locality": "Hunters Creek", "rio_city": "Orlando",
            "state": "FL", "relationship": "LOCATED_IN", "source_run_date": "20260919"})
    with open(tmp_path / "locality_city_duplicates.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["school_id", "locality", "rio_city", "state",
                                          "source_run_date", "note"])
        w.writeheader()
        w.writerow({"school_id": "7440", "locality": "Jacksonville", "rio_city": "Jacksonville",
                    "state": "FL", "source_run_date": "20260919", "note": "city-fallback"})
    (tmp_path / "locality_unresolved.csv").write_text("school_id,reason,detail\n", encoding="utf-8")
    (tmp_path / "nearby_diagnostic_set.json").write_text("{}", encoding="utf-8")
    return LocalityResolver(tmp_path)


@pytest.fixture
def checker(resolver):
    return DeterministicChecker(PARAMS, BRAND_CFG, resolver)


class TestKeywords:
    def test_prek_variants(self, checker):
        r = checker.evaluate(extracted("Our PreK program and pre-kindergarten readiness work."), SCHOOL)
        assert r["KW_02"]["welcome_status"] == "PRESENT"
        assert r["KW_02"]["counts"]["welcome"] == 2

    def test_drop_in_for_a_tour_is_not_flexcare(self, checker):
        r = checker.evaluate(extracted("Stop by or drop in for a tour today!"), SCHOOL)
        assert r["KW_05"]["welcome_status"] == "ABSENT"

    def test_drop_in_care_is_flexcare(self, checker):
        r = checker.evaluate(extracted("We offer drop-in care for busy families."), SCHOOL)
        assert r["KW_05"]["welcome_status"] == "PRESENT"

    def test_affordability_is_not_affordable(self, checker):
        r = checker.evaluate(extracted("We believe in affordability for all."), SCHOOL)
        assert r["SKW_17"]["welcome_status"] == "ABSENT"
        r = checker.evaluate(extracted("Quality care at an affordable price."), SCHOOL)
        assert r["SKW_17"]["welcome_status"] == "PRESENT"

    def test_density_counts(self, checker):
        r = checker.evaluate(extracted("Great daycare here.", "Our daycare and day care rooms."), SCHOOL)
        assert r["KW_01"]["counts"] == {"welcome": 1, "rest": 2}
        assert r["KW_01"]["welcome_status"] == "PRESENT"
        assert r["KW_01"]["rest_status"] == "PRESENT"

    def test_near_you(self, checker):
        r = checker.evaluate(extracted("", "The best preschool near you."), SCHOOL)
        assert r["SKW_16"]["rest_status"] == "PRESENT"
        assert r["SKW_16"]["welcome_status"] == "ABSENT"
        assert r["SKW_16"]["page_status"] == "PRESENT"


class TestTech:
    def test_sproutabout_with_and_without_r(self, checker):
        r = checker.evaluate(extracted("Download our SproutAbout® app today."), SCHOOL)
        assert r["TECH_01"]["welcome_status"] == "PRESENT"
        r = checker.evaluate(extracted("Download SproutAbout for live streaming."), SCHOOL)
        assert r["TECH_01"]["welcome_status"] == "PRESENT"

    def test_montessori_brand_expects_compass(self, checker):
        mu = dict(SCHOOL, brand="Montessori Unlimited")
        r = checker.evaluate(extracted("Track the day with Montessori Compass."), mu)
        assert r["TECH_01"]["welcome_status"] == "PRESENT"
        r = checker.evaluate(extracted("Download SproutAbout for live streaming."), mu)
        assert r["TECH_01"]["welcome_status"] == "ABSENT"


class TestStructural:
    def test_str01_bands(self, checker):
        good = " ".join(["word"] * 214)
        r = checker.evaluate(extracted(good), SCHOOL)
        assert r["STR_01"]["welcome_status"] == "PRESENT"
        partial = " ".join(["word"] * 130)
        r = checker.evaluate(extracted(partial), SCHOOL)
        assert r["STR_01"]["welcome_status"] == "PARTIAL"
        short = " ".join(["word"] * 90)
        r = checker.evaluate(extracted(short), SCHOOL)
        assert r["STR_01"]["welcome_status"] == "ABSENT"

    def test_str01_no_welcome(self, checker):
        r = checker.evaluate(extracted("", "page content only", found=False), SCHOOL)
        assert r["STR_01"]["welcome_status"] == "ABSENT"
        assert "NO_WELCOME_SECTION" in r["STR_01"]["flags"]
        assert r["STR_01"]["page_status"] == "N_A"  # welcome-section property

    def test_str02_city_in_opening(self, checker):
        w = ("Welcome to our school on Beach Boulevard in Jacksonville, Florida! "
             "Since 1991 we have served families. Our teachers are wonderful.")
        r = checker.evaluate(extracted(w), SCHOOL)
        assert r["STR_02"]["welcome_status"] == "PRESENT"

    def test_str02_city_only_in_third_sentence(self, checker):
        w = ("Welcome to our wonderful school! We love every child dearly. "
             "Families in Jacksonville choose us.")
        r = checker.evaluate(extracted(w), SCHOOL)
        assert r["STR_02"]["welcome_status"] == "ABSENT"


class TestGeoWiring:
    def test_geo04_city_duplicate_is_na_both_scopes(self, checker):
        r = checker.evaluate(extracted("daycare in Jacksonville", "more Jacksonville text"), SCHOOL)
        assert r["GEO_04"]["welcome_status"] == "N_A"
        assert r["GEO_04"]["rest_status"] == "N_A"
        assert r["GEO_04"]["page_status"] == "N_A"

    def test_geo04_register_school(self, checker):
        s = dict(SCHOOL, school_id="5001", city="Orlando")
        r = checker.evaluate(
            {"school_id": "5001", "welcome_found": True,
             "welcome_text": "In the heart of Hunters Creek, Orlando.",
             "welcome_word_count": 7, "body_text": "", "body_rest_text": "other text"}, s)
        assert r["GEO_04"]["welcome_status"] == "PRESENT"
        assert r["GEO_04"]["rest_status"] == "ABSENT"
        assert r["GEO_04"]["source_tier"] == "TIER2_REGISTER"

    def test_geo02_scopes(self, checker):
        r = checker.evaluate(extracted("Welcome to our school!", "families in Jacksonville"), SCHOOL)
        assert r["GEO_02"]["welcome_status"] == "ABSENT"
        assert r["GEO_02"]["rest_status"] == "PRESENT"
        assert r["GEO_02"]["page_status"] == "PRESENT"


def test_combine_page_status():
    assert combine_page_status("PRESENT", "ABSENT") == "PRESENT"
    assert combine_page_status("ABSENT", "PARTIAL") == "PARTIAL"
    assert combine_page_status("ABSENT", "ABSENT") == "ABSENT"
    assert combine_page_status("N_A", "N_A") == "N_A"
    assert combine_page_status("ABSENT", "N_A") == "ABSENT"
