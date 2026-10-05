"""S4 unit tests — section extractor v2 channel contract and fallbacks."""

import json

import pytest

from src.scraper.section_extractor import SectionExtractor

NAV = ("<header><nav><a>Find a School</a><a>Schools in Jacksonville</a>"
       "<a>Schools in Orlando</a><a>Schools in Tampa</a></nav></header>")
FOOTER = "<footer>© LCG | Privacy Policy | Corporate: 21333 Haggerty Rd, Novi, MI</footer>"
WELCOME = ("<section><div class='welcomeMessage'><div class='row'>"
           "<div class='welcome-content'><h2>Welcome to Our School</h2>"
           "<div class='welcome-columns'>"
           "<p>Welcome to La Petite Academy on Beach Boulevard in Jacksonville, Florida! "
           "Since 1991 we have provided quality daycare and early education in a safe, "
           "supportive environment for every family we serve each day.</p>"
           "<p>We are conveniently located on Beach Boulevard between Hodges Boulevard "
           "and San Pablo Road, across from Denny's, and we would love to meet you.</p>"
           "</div></div></div></div></section>")
PROGRAMS = ("<section><h2>Daycare and Early Education Programs</h2>"
            "<p>Our programs include Infants, Toddlers, Twos, Preschool and Pre-K "
            "with enrichment options for every age group in our care.</p></section>")
CAROUSEL = ("<section>"
            "<div class='slide'><h2>Meet Our Staff</h2><p>Dee Pryor, Director, brings "
            "fifteen years of early education experience to our school family.</p></div>"
            "<div class='slide'><h2>Meet Our Staff</h2><p>Dee Pryor, Director, brings "
            "fifteen years of early education experience to our school family.</p></div>"
            "</section>")


def page(*parts):
    return "<html><body>" + "".join(parts) + "</body></html>"


@pytest.fixture
def extractor(tmp_path):
    return SectionExtractor(extracted_dir=tmp_path)


class TestChannels:
    def test_template_page(self, extractor):
        out = extractor.extract("t1", page(NAV, WELCOME, PROGRAMS, FOOTER),
                                brand="La Petite Academy")
        assert out["welcome_found"] is True
        assert out["extraction_method"] == "BRAND_SELECTOR"
        assert "Beach Boulevard" in out["welcome_text"]
        # nav cities live ONLY in the boilerplate channel
        assert "Orlando" not in out["body_text"]
        assert "Orlando" in out["boilerplate_text"]
        # footer corporate address must not leak into body
        assert "Haggerty" not in out["body_text"]

    def test_body_rest_excludes_welcome(self, extractor):
        out = extractor.extract("t2", page(NAV, WELCOME, PROGRAMS, FOOTER),
                                brand="La Petite Academy")
        assert "Denny's" in out["welcome_text"]
        assert "Denny's" in out["body_text"]          # whole-page scope includes welcome
        assert "Denny's" not in out["body_rest_text"]  # "elsewhere" scope excludes it
        assert "Preschool" in out["body_rest_text"]

    def test_no_welcome_means_not_found_never_fake(self, extractor):
        out = extractor.extract("t3", page(NAV, PROGRAMS, FOOTER), brand="La Petite Academy")
        assert out["welcome_found"] is False
        assert out["extraction_method"] == "NOT_FOUND"
        assert out["welcome_text"] == ""
        assert out["welcome_word_count"] == 0
        assert out["body_rest_text"] == out["body_text"]

    def test_heading_walk_without_template_class(self, extractor):
        html = page(
            NAV,
            "<section><div class='intro'><h2>Welcome to Our School</h2>"
            "<p>Our Jacksonville school on Beach Boulevard has served families since 1991 "
            "with dedicated teachers, engaging programs, and a warm welcoming environment "
            "that children love coming back to every single day.</p></div></section>",
            PROGRAMS, FOOTER)
        out = extractor.extract("t4", html, brand="Some New Brand")
        assert out["welcome_found"] is True
        assert out["extraction_method"] == "HEADING_WALK"
        assert "Beach Boulevard" in out["welcome_text"]

    def test_carousel_blocks_deduplicated(self, extractor):
        out = extractor.extract("t5", page(NAV, WELCOME, CAROUSEL, FOOTER),
                                brand="La Petite Academy")
        assert out["body_text"].count("Dee Pryor") == 1

    def test_oversized_container_rejected(self, extractor):
        # Welcome heading whose only ancestors wrap the ENTIRE page, and whose
        # sibling run would also swallow everything -> must not pretend the
        # whole page is the Welcome section.
        huge = "<p>" + "filler words here and more. " * 120 + "</p>"
        html = ("<html><body><div class='wrapper'><h2>Welcome to Our School</h2>"
                + huge + huge + huge + "</div></body></html>")
        out = extractor.extract("t6", html, brand="X")
        if out["welcome_found"]:
            assert out["welcome_word_count"] <= 0.6 * out["body_word_count"]
        else:
            assert out["welcome_text"] == ""

    def test_output_persisted(self, tmp_path):
        ex = SectionExtractor(extracted_dir=tmp_path)
        ex.extract("t7", page(WELCOME), brand="La Petite Academy")
        saved = json.loads((tmp_path / "t7.json").read_text(encoding="utf-8"))
        assert saved["welcome_found"] is True
