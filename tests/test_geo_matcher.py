"""S2 unit tests — GeoMatcher (GEO_01/02/03/05) and LocalityResolver (GEO_04).

Covers the ARCHITECTURE.md §3 decision matrix and every trap verified live:
compound-prefix cities, guarded state abbreviations, USPS suffix folding,
apostrophe-less brand strings, register/N-A/conflict cohorts.
"""

import csv
import json

import pytest

from src.matching.geo_matcher import GeoMatcher, LocalityResolver

BRAND_CFG = {
    "brands": {
        "La Petite Academy": {"display_name": "La Petite Academy", "aliases": ["La Petite"]},
        "The Childrens Courtyard": {
            "display_name": "The Children's Courtyard",
            "aliases": ["The Children's Courtyard", "Children's Courtyard"],
        },
    }
}


def school(**over):
    base = {
        "school_id": "7440",
        "brand": "La Petite Academy",
        "school_name": "La Petite Academy on Beach Blvd",
        "street_address": "7801 Beach Blvd",
        "city": "Jacksonville",
        "state": "FL",
    }
    base.update(over)
    return base


# ------------------------------------------------------------------- GEO_02
class TestCity:
    def test_present_simple(self):
        r = GeoMatcher(school(), BRAND_CFG).check_city("quality daycare in Jacksonville for families")
        assert r.status == "PRESENT" and "Jacksonville" in r.evidence

    def test_port_charlotte_is_not_charlotte(self):
        r = GeoMatcher(school(city="Charlotte"), BRAND_CFG).check_city(
            "our school serves Port Charlotte families")
        assert r.status == "ABSENT"
        assert "AMBIGUOUS_PREFIX_SKIPPED" in r.flags

    def test_compound_city_matches_itself(self):
        r = GeoMatcher(school(city="North Chili"), BRAND_CFG).check_city(
            "welcome to our North Chili school")
        assert r.status == "PRESENT"

    def test_saint_paul_matches_st_paul(self):
        r = GeoMatcher(school(city="Saint Paul"), BRAND_CFG).check_city(
            "proudly serving St. Paul families")
        assert r.status == "PRESENT"

    def test_other_occurrence_still_counts_after_prefixed_one(self):
        r = GeoMatcher(school(city="Charlotte"), BRAND_CFG).check_city(
            "from Port Charlotte to Charlotte proper, families choose us")
        assert r.status == "PRESENT"


# ------------------------------------------------------------------- GEO_03
class TestState:
    def test_fl_never_matches_flexible(self):
        r = GeoMatcher(school(), BRAND_CFG).check_state(
            "we offer flexible scheduling and flexcare options")
        assert r.status == "ABSENT"

    def test_comma_context(self):
        r = GeoMatcher(school(), BRAND_CFG).check_state("located in Jacksonville, FL near the beach")
        assert r.status == "PRESENT"

    def test_zip_context(self):
        r = GeoMatcher(school(), BRAND_CFG).check_state("7801 Beach Blvd FL 32216")
        assert r.status == "PRESENT"

    def test_full_name(self):
        r = GeoMatcher(school(), BRAND_CFG).check_state("the best education-driven child care in Florida")
        assert r.status == "PRESENT"

    def test_city_adjacent_abbr(self):
        r = GeoMatcher(school(), BRAND_CFG).check_state("our Jacksonville FL campus")
        assert r.status == "PRESENT"

    def test_allcaps_in_is_not_indiana(self):
        r = GeoMatcher(school(state="IN", city="Carmel"), BRAND_CFG).check_state(
            "COME JOIN THE DROP IN FUN TODAY")
        assert r.status == "ABSENT"

    def test_indiana_comma_context(self):
        r = GeoMatcher(school(state="IN", city="Carmel"), BRAND_CFG).check_state(
            "visit us in Carmel, IN this week")
        assert r.status == "PRESENT"

    def test_indianapolis_does_not_satisfy_indiana_name(self):
        r = GeoMatcher(school(state="IN", city="Carmel"), BRAND_CFG).check_state(
            "our Indianapolis friends")
        assert r.status == "ABSENT"

    def test_uncontexted_uppercase_abbr_is_conservatively_absent(self):
        r = GeoMatcher(school(), BRAND_CFG).check_state("VISIT FL TODAY")
        assert r.status == "ABSENT"


# ------------------------------------------------------------------- GEO_01
class TestSchoolName:
    def test_present_with_suffix_class_folding(self):
        r = GeoMatcher(school(), BRAND_CFG).check_school_name(
            "Welcome to La Petite Academy on Beach Boulevard in Jacksonville, Florida!")
        assert r.status == "PRESENT"

    def test_brand_only_is_partial(self):
        r = GeoMatcher(school(), BRAND_CFG).check_school_name(
            "La Petite Academy offers the best education-driven child care")
        assert r.status == "PARTIAL" and "BRAND_ONLY" in r.flags

    def test_absent(self):
        r = GeoMatcher(school(), BRAND_CFG).check_school_name(
            "a wonderful place for early learning")
        assert r.status == "ABSENT"

    def test_apostropheless_rio_brand_matches_site_spelling(self):
        s = school(
            school_id="3034", brand="The Childrens Courtyard",
            school_name="The Childrens Courtyard on Willow Grove Dr",
            street_address="2155 Willow Grove Dr", city="Lewisville", state="TX",
        )
        r = GeoMatcher(s, BRAND_CFG).check_school_name(
            "The Children's Courtyard on Willow Grove Drive welcomes you")
        assert r.status == "PRESENT"

    def test_separated_components_partial(self):
        r = GeoMatcher(school(), BRAND_CFG).check_school_name(
            "La Petite Academy is a great place. Our campus sits just off "
            "the busy corridor, and families love it. You will find us near "
            "the shops and right on Beach Blvd with easy access.")
        assert r.status == "PARTIAL" and "NAME_COMPONENTS_SEPARATED" in r.flags


# ------------------------------------------------------------------- GEO_05
class TestStreet:
    def test_suffix_class_boulevard(self):
        r = GeoMatcher(school(), BRAND_CFG).check_street(
            "conveniently located on Beach Boulevard between Hodges Boulevard and San Pablo Road")
        assert r.status == "PRESENT"

    def test_abbreviated_suffix(self):
        r = GeoMatcher(school(), BRAND_CFG).check_street("find us at 7801 Beach Blvd.")
        assert r.status == "PRESENT"

    def test_single_core_without_suffix_not_credited(self):
        r = GeoMatcher(school(), BRAND_CFG).check_street(
            "we love trips to the beach every summer")
        assert r.status == "ABSENT"

    def test_directional_and_suffix_variants(self):
        s = school(street_address="12336 North Mopac Expressway", city="Austin", state="TX")
        r = GeoMatcher(s, BRAND_CFG).check_street("just off N Mopac Expwy near the Domain")
        assert r.status == "PRESENT"

    def test_directional_optional(self):
        s = school(street_address="12336 North Mopac Expressway", city="Austin", state="TX")
        r = GeoMatcher(s, BRAND_CFG).check_street("located on Mopac Expressway")
        assert r.status == "PRESENT"

    def test_multiword_core_without_suffix_is_partial(self):
        s = school(street_address="2155 Willow Grove Dr", city="Lewisville", state="TX")
        r = GeoMatcher(s, BRAND_CFG).check_street("in the heart of the Willow Grove community")
        assert r.status == "PARTIAL" and "STREET_CORE_ONLY" in r.flags

    def test_trailing_directional(self):
        s = school(street_address="1000 Watermill Blvd S")
        r = GeoMatcher(s, BRAND_CFG).check_street("on Watermill Boulevard South in Jacksonville")
        assert r.status == "PRESENT"


# ------------------------------------------------------------------- GEO_04
@pytest.fixture
def locality_data(tmp_path):
    reg = tmp_path / "verified_locality_register.csv"
    cols = ["school_id", "locality", "locality_aliases", "rio_city", "state",
            "relationship", "distance_mi", "source_run_date", "source_workbook",
            "seo_flags", "auto_review_flags", "snapshot_hash",
            "approved", "approved_by", "approved_date", "analyst_notes"]
    rows = [
        {"school_id": "3236", "locality": "Bridle Trails", "locality_aliases": "",
         "rio_city": "Bellevue", "state": "WA", "relationship": "LOCATED_IN",
         "distance_mi": "", "source_run_date": "20260919", "source_workbook": "x.xlsx",
         "seo_flags": "", "auto_review_flags": "", "snapshot_hash": "h",
         "approved": "", "approved_by": "", "approved_date": "", "analyst_notes": ""},
        {"school_id": "3053", "locality": "Valley Ranch", "locality_aliases": "",
         "rio_city": "Irving", "state": "TX", "relationship": "LOCATED_IN",
         "distance_mi": "", "source_run_date": "20260919", "source_workbook": "x.xlsx",
         "seo_flags": "", "auto_review_flags": "", "snapshot_hash": "h",
         "approved": "yes", "approved_by": "analyst", "approved_date": "2026-10-06",
         "analyst_notes": ""},
    ]
    with open(reg, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    with open(tmp_path / "locality_city_duplicates.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["school_id", "locality", "rio_city", "state",
                                          "source_run_date", "note"])
        w.writeheader()
        w.writerow({"school_id": "3034", "locality": "Lewisville", "rio_city": "Lewisville",
                    "state": "TX", "source_run_date": "20260919", "note": "city-fallback"})

    with open(tmp_path / "locality_unresolved.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["school_id", "reason", "detail"])
        w.writeheader()
        w.writerow({"school_id": "9999", "reason": "NO_LOCATED_IN", "detail": "rural"})

    (tmp_path / "nearby_diagnostic_set.json").write_text(json.dumps({
        "3236": [{"name": "Kirkland", "distance_mi": 6.2, "relationship": "NEARBY"}],
    }), encoding="utf-8")
    return tmp_path


class TestLocalityResolver:
    def test_register_present_pending_approval(self, locality_data):
        lr = LocalityResolver(locality_data)
        r = lr.check("3236", "nestled in the Bridle Trails neighborhood of Bellevue", "Bellevue")
        assert r.status == "PRESENT"
        assert r.source_tier == "TIER2_REGISTER"
        assert "PENDING_G1_APPROVAL" in r.flags

    def test_register_absent(self, locality_data):
        lr = LocalityResolver(locality_data)
        r = lr.check("3236", "a wonderful Bellevue school", "Bellevue")
        assert r.status == "ABSENT"

    def test_approved_row_has_no_pending_flag(self, locality_data):
        lr = LocalityResolver(locality_data)
        r = lr.check("3053", "right in Valley Ranch, Irving", "Irving")
        assert r.status == "PRESENT" and "PENDING_G1_APPROVAL" not in r.flags

    def test_nearby_mention_is_flag_only_never_credit(self, locality_data):
        lr = LocalityResolver(locality_data)
        r = lr.check("3236", "serving families from Kirkland and beyond", "Bellevue")
        assert r.status == "ABSENT"  # Kirkland is >5mi NEARBY: zero credit
        assert any(f.startswith("FAR_LOCALITY_MENTION:Kirkland") for f in r.flags)

    def test_city_duplicate_cohort_is_na(self, locality_data):
        lr = LocalityResolver(locality_data)
        r = lr.check("3034", "the best daycare in Lewisville", "Lewisville")
        assert r.status == "N_A"

    def test_unresolved_cohort_is_na_with_flag(self, locality_data):
        lr = LocalityResolver(locality_data)
        r = lr.check("9999", "any text", "Somewhere")
        assert r.status == "N_A"
        assert any(f.startswith("UNRESOLVED_LOCALITY_DATA") for f in r.flags)

    def test_unknown_school_is_na(self, locality_data):
        lr = LocalityResolver(locality_data)
        r = lr.check("12345", "any text")
        assert r.status == "N_A"
