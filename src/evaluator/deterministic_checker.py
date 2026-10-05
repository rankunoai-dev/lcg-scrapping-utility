"""S5 — Deterministic evaluator v2, wired to the S2 matchers.

Covers every parameter with a deterministic first pass:
  GEO_01/02/03/05   GeoMatcher vs Tier-0 RIO ground truth
  GEO_04            LocalityResolver (Verified Locality Register, §3 matrix)
  KW_01–05          head keywords: word-boundary variants from config + counts
  SKW_01–17         semantic keywords: same engine (density profile)
  TECH_01           brand rule (SproutAbout / Montessori Compass, no ® required)
  STR_01 / STR_02   structural rules on the Welcome section

Scope model (ARCHITECTURE §4.1):
  welcome scope = welcome_text
  rest scope    = body_rest_text  (page minus welcome — the "elsewhere" axis)
  page_status   = combine(welcome, rest); the four-state placement model uses
                  welcome_status × rest_status, which keeps WELCOME_ONLY reachable.

STR_01/STR_02 are properties OF the welcome section: their rest/page scope is
N_A by design (excluded from page denominators).

Every result carries evidence, source tier, provenance, flags, and per-scope
occurrence counts for keywords. Hybrid parameters evaluated here may later be
UPGRADED from ABSENT to PARTIAL by the semantic pass (S6) — never downgraded.
"""

from __future__ import annotations

import re

from src.matching.geo_matcher import GeoMatcher, LocalityResolver, MatchResult
from src.matching.normalize import fold_text, phrase_pattern, snippet_around

CHECKER_VERSION = "2.0.0"
_SENTENCE_RX = re.compile(r"(?<=[.!?])\s+")

_STATUS_RANK = {"PRESENT": 3, "PARTIAL": 2, "ABSENT": 1, "N_A": 0}


def combine_page_status(welcome: str, rest: str) -> str:
    """Whole-page presence from the two sub-scopes."""
    if welcome == "N_A" and rest == "N_A":
        return "N_A"
    best = max((s for s in (welcome, rest) if s != "N_A"),
               key=lambda s: _STATUS_RANK[s])
    return best


class DeterministicChecker:
    def __init__(self, params_config: list[dict], brand_cfg: dict,
                 locality_resolver: LocalityResolver | None = None):
        self.params = {p["id"]: p for p in params_config}
        self.brand_cfg = brand_cfg
        self.resolver = locality_resolver or LocalityResolver()

    # ------------------------------------------------------------ keyword
    def _count_variants(self, variants: list[str], folded: str):
        """(count, first_evidence) across variants, overlapping spans deduped."""
        spans: set[tuple[int, int]] = set()
        first = None
        for v in variants:
            for m in phrase_pattern(v).finditer(folded):
                span = (m.start(), m.end())
                if any(s <= span[0] < e or s < span[1] <= e for s, e in spans):
                    continue
                spans.add(span)
                if first is None or span[0] < first[0]:
                    first = span
        evidence = snippet_around(folded, *first) if first else None
        return len(spans), evidence

    def _keyword_result(self, param: dict, welcome: str, rest: str) -> dict:
        variants = param.get("variants") or [param["name"]]
        wc, wev = self._count_variants(variants, welcome)
        rc, rev = self._count_variants(variants, rest)
        ws = "PRESENT" if wc else "ABSENT"
        rs = "PRESENT" if rc else "ABSENT"
        return self._pack(
            ws, rs, wev, rev,
            source_tier="DETERMINISTIC_KEYWORD",
            provenance=f"Word-boundary variants: {variants}",
            counts={"welcome": wc, "rest": rc},
        )

    # -------------------------------------------------------------- pack
    @staticmethod
    def _pack(welcome_status, rest_status, welcome_evidence, page_evidence,
              source_tier="", provenance="", flags=None, counts=None) -> dict:
        return {
            "welcome_status": welcome_status,
            "rest_status": rest_status,
            "page_status": combine_page_status(welcome_status, rest_status),
            "welcome_evidence": welcome_evidence,
            # page scope includes the welcome: when page presence derives from
            # a welcome-only hit, the welcome evidence IS the page evidence
            # (validator rule V6: no PRESENT/PARTIAL without evidence).
            "page_evidence": page_evidence if page_evidence is not None else welcome_evidence,
            "source_tier": source_tier,
            "provenance": provenance,
            "flags": flags or [],
            "counts": counts or {},
            "method": "DETERMINISTIC",
            "checker_version": CHECKER_VERSION,
        }

    @staticmethod
    def _from_matches(wel: MatchResult, rest: MatchResult) -> dict:
        return DeterministicChecker._pack(
            wel.status, rest.status, wel.evidence, rest.evidence or wel.evidence,
            source_tier=wel.source_tier or rest.source_tier,
            provenance=wel.provenance or rest.provenance,
            flags=sorted(set(wel.flags) | set(rest.flags)),
        )

    # ------------------------------------------------------------ evaluate
    def evaluate(self, extracted: dict, school: dict) -> dict[str, dict]:
        welcome = extracted.get("welcome_text", "") or ""
        rest = extracted.get("body_rest_text", "") or ""
        welcome_found = bool(extracted.get("welcome_found"))
        school_id = str(school.get("school_id", extracted.get("school_id", "")))

        gm = GeoMatcher(school, self.brand_cfg)
        results: dict[str, dict] = {}

        # --- GEO via matchers -------------------------------------------
        results["GEO_01"] = self._from_matches(gm.check_school_name(welcome),
                                               gm.check_school_name(rest))
        results["GEO_02"] = self._from_matches(gm.check_city(welcome), gm.check_city(rest))
        results["GEO_03"] = self._from_matches(gm.check_state(welcome), gm.check_state(rest))
        results["GEO_04"] = self._from_matches(
            self.resolver.check(school_id, welcome, school.get("city", "")),
            self.resolver.check(school_id, rest, school.get("city", "")))
        results["GEO_05"] = self._from_matches(gm.check_street(welcome), gm.check_street(rest))

        # --- keywords (head + semantic) ----------------------------------
        for pid, param in self.params.items():
            if param["category"] in ("KEYWORDS_HEAD", "SEMANTIC_KEYWORDS"):
                results[pid] = self._keyword_result(param, fold_text(welcome), fold_text(rest))

        # --- TECH_01 brand rule ------------------------------------------
        brand = school.get("brand", "")
        app = self.brand_cfg.get("brands", {}).get(brand, {}).get("app", "SproutAbout")
        terms = self.brand_cfg.get("tech_detection_terms", {}).get(app, [app.lower()])
        wc_, wev = self._count_variants(terms, fold_text(welcome))
        rc_, rev = self._count_variants(terms, fold_text(rest))
        results["TECH_01"] = self._pack(
            "PRESENT" if wc_ else "ABSENT", "PRESENT" if rc_ else "ABSENT", wev, rev,
            source_tier="TIER0_BRAND_RULE",
            provenance=f"Brand rule: {brand or '?'} -> {app} (terms: {terms}; ® not required)",
            counts={"welcome": wc_, "rest": rc_},
        )

        # --- STR_01 word count (welcome-scope property) -------------------
        wcnt = extracted.get("welcome_word_count", 0)
        if not welcome_found:
            s1, ev1, fl1 = "ABSENT", "No Welcome section found on page", ["NO_WELCOME_SECTION"]
        elif 180 <= wcnt <= 300:
            s1, ev1, fl1 = "PRESENT", f"Welcome word count {wcnt} (target 180-300)", []
        elif 120 <= wcnt <= 400:
            s1, ev1, fl1 = "PARTIAL", f"Welcome word count {wcnt} (outside 180-300, within 120-400)", []
        else:
            s1, ev1, fl1 = "ABSENT", f"Welcome word count {wcnt} (outside 120-400)", []
        results["STR_01"] = self._pack(s1, "N_A", ev1, None,
                                       source_tier="STRUCTURAL",
                                       provenance="Welcome-section property; page scope N_A",
                                       flags=fl1)
        # STR rules are welcome-section properties: they must never enter the
        # page-score denominator, whatever the welcome verdict is.
        results["STR_01"]["page_status"] = "N_A"

        # --- STR_02 leads with local info ---------------------------------
        if not welcome_found:
            results["STR_02"] = self._pack("ABSENT", "N_A",
                                           "No Welcome section found on page", None,
                                           source_tier="STRUCTURAL",
                                           provenance="Welcome-section property; page scope N_A",
                                           flags=["NO_WELCOME_SECTION"])
        else:
            opening = " ".join(_SENTENCE_RX.split(fold_text(welcome))[:2])
            hits = []
            if gm.check_city(opening).status == "PRESENT":
                hits.append("city")
            if gm.check_street(opening).status in ("PRESENT", "PARTIAL"):
                hits.append("street")
            loc = self.resolver.check(school_id, opening, school.get("city", ""))
            if loc.status == "PRESENT":
                hits.append("locality")
            s2 = "PRESENT" if hits else "ABSENT"
            ev2 = (f"Opening 2 sentences contain local signal(s) {hits}: "
                   f"'{opening[:140]}...'") if hits else \
                  f"No city/street/locality in opening 2 sentences: '{opening[:140]}...'"
            results["STR_02"] = self._pack(s2, "N_A", ev2, None,
                                           source_tier="STRUCTURAL",
                                           provenance="Deterministic scan of first 2 Welcome "
                                                      "sentences (city/street/locality)",
                                           flags=[])
        results["STR_02"]["page_status"] = "N_A"
        return results
