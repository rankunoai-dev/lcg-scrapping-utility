"""Tests for the client CMS welcome source (Tier-0 welcome scope)."""

from src.ingest.welcome_source import (_norm_sid, _token_overlap,
                                       apply_client_welcome)

CLIENT = ("Welcome to La Petite Academy on Beach Boulevard in Jacksonville, "
          "Florida! Since 1991 we have provided quality daycare and early "
          "education in a safe, supportive environment.")


def extracted(welcome="", found=None, rest="rest text"):
    if found is None:
        found = bool(welcome)
    return {"school_id": "7440", "welcome_found": found, "welcome_text": welcome,
            "welcome_word_count": len(welcome.split()), "body_rest_text": rest,
            "extraction_method": "BRAND_SELECTOR" if found else "NOT_FOUND",
            "welcome_dom_path": "x"}


def test_norm_sid_strips_zero_padding():
    assert _norm_sid("0002") == "2"
    assert _norm_sid(7440) == "7440"
    assert _norm_sid("ABC") == "ABC"


def test_client_text_overrides_scraped():
    ex = apply_client_welcome(extracted("Welcome to Our School " + CLIENT), CLIENT)
    assert ex["welcome_text"] == CLIENT
    assert ex["welcome_source"] == "CLIENT_FILE"
    assert ex["extraction_method"] == "CLIENT_FILE"
    assert ex["welcome_found"] is True
    assert ex["welcome_source_flags"] == []  # near-identical text: no mismatch


def test_mismatch_flagged_when_page_differs():
    stale = ("Our brand new campus in Orlando offers enrichment clubs and a "
             "huge indoor gym for every age group with certified teachers "
             "and daily progress updates for all our wonderful families.")
    ex = apply_client_welcome(extracted(stale), CLIENT)
    assert ex["welcome_text"] == CLIENT  # client file still wins
    assert "WELCOME_CMS_PAGE_MISMATCH" in ex["welcome_source_flags"]


def test_rest_overlap_flag_when_scraper_found_nothing():
    ex = apply_client_welcome(extracted("", found=False), CLIENT)
    assert ex["welcome_found"] is True
    assert "REST_MAY_INCLUDE_WELCOME" in ex["welcome_source_flags"]


def test_fallback_to_scraped_when_school_missing_from_file():
    ex = apply_client_welcome(extracted("Scraped welcome words here for a "
                                        "school absent from the file."), None)
    assert ex["welcome_source"] == "SCRAPED_FALLBACK"
    assert ex["welcome_text"].startswith("Scraped welcome")
    assert ex["extraction_method"] == "BRAND_SELECTOR"  # untouched


def test_no_welcome_any_source():
    ex = apply_client_welcome(extracted("", found=False), None)
    assert ex["welcome_source"] == "NONE"
    assert "NO_WELCOME_ANY_SOURCE" in ex["welcome_source_flags"]


def test_token_overlap_symmetric_bounds():
    assert _token_overlap(CLIENT, CLIENT) == 1.0
    assert _token_overlap(CLIENT, "") == 0.0
