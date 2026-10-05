"""S2 unit tests — normalization primitives and the verified matching traps."""

from src.matching.normalize import (
    fold_text, norm_key, norm_zip, phrase_pattern, slug_to_key, tokens_of,
)


# ---------------------------------------------------------------- fold / keys
def test_fold_curly_apostrophe():
    assert fold_text("across from Denny’s") == "across from Denny's"


def test_norm_key_apostrophe_variants_equal():
    # RIO stores "The Childrens Courtyard"; the site writes "The Children's Courtyard"
    assert norm_key("The Childrens Courtyard") == norm_key("The Children's Courtyard")


def test_norm_key_saint_folding():
    assert norm_key("St. Paul") == norm_key("Saint Paul") == slug_to_key("saint-paul")


def test_norm_key_land_o_lakes_slug():
    assert norm_key("Land O' Lakes") == slug_to_key("land-o-lakes")


def test_norm_zip_leading_zero_and_plus4():
    assert norm_zip(6010) == "06010"
    assert norm_zip("28202-1234") == "28202"
    assert norm_zip("76155.0") == "76155"
    assert norm_zip(None) == ""


# ------------------------------------------------------------- phrase_pattern
def test_state_substring_trap_fl_not_in_flexible():
    # The bug that invalidated the original checker: "fl" in "flexible".
    assert phrase_pattern("FL").search("We offer flexible scheduling") is None


def test_boundary_trap_in_or_me():
    assert phrase_pattern("IN").search("individualized learning included") is None
    assert phrase_pattern("OR").search("organized orderly classrooms") is None
    assert phrase_pattern("ME").search("merry memorable moments") is None


def test_hyphenated_city_matches_spaced_and_hyphenated():
    pat = phrase_pattern("Winston-Salem")
    assert pat.search("families across Winston Salem love us")
    assert pat.search("located in winston-salem today")
    assert pat.search("the winstonsalem area") is None  # fused form is a different token


def test_apostrophe_tolerant_token():
    pat = phrase_pattern("Denny's")
    assert pat.search(fold_text("right across from Denny’s on Beach Blvd"))
    assert pat.search("across from Dennys restaurant")


def test_ofallon_apostrophe():
    assert phrase_pattern("O'Fallon").search("our O'Fallon location")
    assert phrase_pattern("O'Fallon").search("our OFallon location")


def test_full_state_name_not_inside_indianapolis():
    assert phrase_pattern("Indiana").search("our Indianapolis campus") is None
    assert phrase_pattern("Indiana").search("serving Indiana families")


def test_st_period_separator():
    assert phrase_pattern("St. Paul").search("families in St. Paul and beyond")
    assert phrase_pattern("St. Paul").search("families in St Paul")


def test_tokens_of():
    assert tokens_of("O'Fallon") == ["ofallon"]
    assert tokens_of("St. Paul") == ["st", "paul"]
    assert tokens_of("12336 North Mopac Expressway") == ["12336", "north", "mopac", "expressway"]
