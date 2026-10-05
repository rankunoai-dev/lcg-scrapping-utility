"""S2 gate fixture — nav/footer text must never reach the geo checks.

The extractor (S4) emits three channels per school:
    welcome_text   the Welcome section only
    body_text      main content with header/nav/footer/cookie/nearby-school
                   widgets stripped
    boilerplate_text   everything stripped out of body_text

The matching contract pinned here: evaluators call GeoMatcher/LocalityResolver
with welcome_text or body_text ONLY. A city that appears solely in the nav
(boilerplate channel) therefore cannot score — which this test demonstrates
end-to-end on a fixture shaped like a real extraction output.
"""

from src.matching.geo_matcher import GeoMatcher

BRAND_CFG = {"brands": {"La Petite Academy": {"display_name": "La Petite Academy", "aliases": []}}}

# Shaped like section_extractor v2 output for a Jacksonville school whose nav
# lists sibling cities (the verified false-positive source on brand sites).
EXTRACTED = {
    "school_id": "7440",
    "welcome_text": "Welcome to our school on Beach Boulevard! Since 1991 we have "
                    "provided quality daycare in a safe, supportive environment.",
    "body_text": "Our programs include Infants, Toddlers, Twos, Preschool and Pre-K. "
                 "Schedule a tour today - we can't wait to meet your family!",
    "boilerplate_text": "Find a School | Schools in Jacksonville | Schools in Orlando | "
                        "Schools in Tampa | Schools in Miami | Careers | Privacy Policy",
}

SCHOOL = {
    "school_id": "7440", "brand": "La Petite Academy",
    "school_name": "La Petite Academy on Beach Blvd",
    "street_address": "7801 Beach Blvd", "city": "Jacksonville", "state": "FL",
}


def test_city_only_in_nav_does_not_count():
    gm = GeoMatcher(SCHOOL, BRAND_CFG)
    # Correct usage: evaluators pass the body/welcome channels.
    assert gm.check_city(EXTRACTED["welcome_text"]).status == "ABSENT"
    assert gm.check_city(EXTRACTED["body_text"]).status == "ABSENT"
    # The nav DOES contain the city — proving the separation is what protects us.
    assert gm.check_city(EXTRACTED["boilerplate_text"]).status == "PRESENT"


def test_street_scores_from_welcome_channel():
    gm = GeoMatcher(SCHOOL, BRAND_CFG)
    assert gm.check_street(EXTRACTED["welcome_text"]).status == "PRESENT"
    assert gm.check_street(EXTRACTED["boilerplate_text"]).status == "ABSENT"
