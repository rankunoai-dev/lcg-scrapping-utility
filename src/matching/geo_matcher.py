"""Deterministic GEO parameter matching (GEO_01–GEO_05) against Tier-0/Tier-2 truth.

Every public check returns a MatchResult carrying status, verbatim evidence
from the (folded) input text, the trust tier of the ground truth used, a
human-readable provenance string, and machine flags. No check here ever
*invents* a location value: GEO_01/02/03/05 match Tier-0 RIO fields,
GEO_04 consults only the Verified Locality Register artifacts built by
src/ingest/locality_register.py (ARCHITECTURE.md §2–§3).

Scope handling is the caller's job: pass welcome_text or body_text; nav and
footer channels must never be passed to these checks.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.matching.normalize import (
    COMPOUND_CITY_PREFIXES, DIRECTIONAL_CLASSES, DIRECTIONAL_LOOKUP,
    STATES, SUFFIX_CLASSES, SUFFIX_LOOKUP, _SEP,
    fold_text, norm_key, phrase_pattern, snippet_around, tokens_of,
)

# token -> acceptable variants, usable by any place-name match
_DIR_EXPANSIONS = {v: DIRECTIONAL_CLASSES[cls] for v, cls in DIRECTIONAL_LOOKUP.items()}
_SAINT_EXPANSIONS = {"st": ["st", "saint"], "saint": ["st", "saint"]}
PLACE_EXPANSIONS = {**_DIR_EXPANSIONS, **_SAINT_EXPANSIONS}
_SUFFIX_EXPANSIONS = {v: SUFFIX_CLASSES[cls] for v, cls in SUFFIX_LOOKUP.items()}
NAME_EXPANSIONS = {**PLACE_EXPANSIONS, **_SUFFIX_EXPANSIONS}

_ANCHOR_STOPWORDS = {"of", "at", "on", "in", "the"}
_WORD_BEFORE_RX = re.compile(r"([A-Za-z0-9']+)\s*$")


@dataclass
class MatchResult:
    status: str                      # PRESENT | PARTIAL | ABSENT | N_A
    evidence: str | None = None
    source_tier: str = ""
    provenance: str = ""
    flags: list[str] = field(default_factory=list)


def _body(term: str, expansions=None) -> str:
    """Pattern body (no boundaries) for composing larger regexes."""
    pat = phrase_pattern(term, expansions).pattern
    return pat[len(r"(?<![A-Za-z0-9])"):-len(r"(?![A-Za-z0-9])")]


class GeoMatcher:
    """Tier-0 checks for one school (a master_schools.csv row)."""

    def __init__(self, school: dict, brand_cfg: dict):
        self.school = school
        self.brand_entry = brand_cfg.get("brands", {}).get(school.get("brand", ""), {})

    # ------------------------------------------------------------------ GEO_02
    def check_city(self, text: str) -> MatchResult:
        city = self.school.get("city", "")
        if not city:
            return MatchResult("ABSENT", source_tier="TIER0_RIO",
                               provenance="RIO City empty", flags=["MISSING_GROUND_TRUTH"])
        folded = fold_text(text)
        pat = phrase_pattern(city, PLACE_EXPANSIONS)
        flags: list[str] = []
        for m in pat.finditer(folded):
            prev = _WORD_BEFORE_RX.search(folded[: m.start()])
            if prev and prev.group(1).replace("'", "").lower() in COMPOUND_CITY_PREFIXES:
                # "Port Charlotte" is not "Charlotte" — skip this occurrence
                # (never fires when the prefix is part of the city itself,
                #  because then the pattern consumed it already).
                flags.append("AMBIGUOUS_PREFIX_SKIPPED")
                continue
            return MatchResult("PRESENT", snippet_around(folded, m.start(), m.end()),
                               "TIER0_RIO", f"RIO City '{city}' (word-boundary, normalized)", flags)
        return MatchResult("ABSENT", None, "TIER0_RIO", f"RIO City '{city}' not found", flags)

    # ------------------------------------------------------------------ GEO_03
    def check_state(self, text: str) -> MatchResult:
        abbr = (self.school.get("state") or "").upper()
        if not abbr:
            return MatchResult("ABSENT", source_tier="TIER0_RIO",
                               provenance="RIO Region empty", flags=["MISSING_GROUND_TRUTH"])
        folded = fold_text(text)
        full_name = STATES.get(abbr)

        if full_name:
            m = phrase_pattern(full_name).search(folded)
            if m:
                return MatchResult("PRESENT", snippet_around(folded, m.start(), m.end()),
                                   "TIER0_RIO", f"State full name '{full_name}'")

        # Abbreviation: uppercase + context only — never bare substrings
        # ("FL" must not fire inside "flexible", nor all-caps "IN" in "DROP IN").
        city_body = _body(self.school.get("city", "zzz_no_city"), PLACE_EXPANSIONS)
        contexts = [
            (rf",\s*{abbr}(?![A-Za-z])", f"comma context ', {abbr}'"),
            (rf"(?<![A-Za-z]){abbr}\s+\d{{5}}(?!\d)", f"'{abbr} <ZIP>' context"),
            (rf"(?i:{city_body})[\s,]+{abbr}(?![A-Za-z])", f"city-adjacent '{abbr}'"),
        ]
        for rx, why in contexts:
            m = re.search(rx, folded)  # deliberately case-sensitive for the abbr
            if m:
                return MatchResult("PRESENT", snippet_around(folded, m.start(), m.end()),
                                   "TIER0_RIO", f"State abbreviation {abbr} ({why})")
        return MatchResult("ABSENT", None, "TIER0_RIO",
                           f"Neither '{full_name}' nor guarded '{abbr}' found")

    # ------------------------------------------------------------------ GEO_01
    def check_school_name(self, text: str) -> MatchResult:
        name = self.school.get("school_name", "")
        if not name:
            return MatchResult("ABSENT", source_tier="TIER0_RIO",
                               provenance="RIO Location Name empty", flags=["MISSING_GROUND_TRUTH"])
        folded = fold_text(text)

        brand_variants = [v for v in dict.fromkeys(
            [self.brand_entry.get("display_name", ""), self.school.get("brand", "")]
            + list(self.brand_entry.get("aliases", []))
        ) if v]

        # Split "La Petite Academy on Beach Blvd" into brand prefix + anchor.
        name_toks = tokens_of(name)
        anchor_toks: list[str] | None = None
        for bv in sorted(brand_variants, key=lambda v: len(tokens_of(v)), reverse=True):
            bt = tokens_of(bv)
            if bt and name_toks[: len(bt)] == bt:
                anchor_toks = name_toks[len(bt):]
                break
        while anchor_toks and anchor_toks[0] in _ANCHOR_STOPWORDS:
            anchor_toks = anchor_toks[1:]

        brand_matches = []
        for bv in brand_variants:
            brand_matches += list(phrase_pattern(bv, NAME_EXPANSIONS).finditer(folded))

        if anchor_toks is None:
            # Location Name does not start with a known brand variant:
            # exact full-name phrase, else brand mention alone.
            m = phrase_pattern(name, NAME_EXPANSIONS).search(folded)
            if m:
                return MatchResult("PRESENT", snippet_around(folded, m.start(), m.end()),
                                   "TIER0_RIO", f"Full Location Name '{name}'")
            if brand_matches:
                b = brand_matches[0]
                return MatchResult("PARTIAL", snippet_around(folded, b.start(), b.end()),
                                   "TIER0_RIO", f"Brand only (Location Name '{name}' not matched)",
                                   ["BRAND_ONLY", "NAME_NO_BRAND_PREFIX"])
            return MatchResult("ABSENT", None, "TIER0_RIO", f"'{name}' not found",
                               ["NAME_NO_BRAND_PREFIX"])

        if not anchor_toks:  # name is exactly the brand
            if brand_matches:
                b = brand_matches[0]
                return MatchResult("PRESENT", snippet_around(folded, b.start(), b.end()),
                                   "TIER0_RIO", f"Brand name '{name}' (no anchor in Location Name)")
            return MatchResult("ABSENT", None, "TIER0_RIO", f"'{name}' not found")

        anchor_pat = phrase_pattern(" ".join(anchor_toks), NAME_EXPANSIONS)
        anchor_matches = list(anchor_pat.finditer(folded))

        for b in brand_matches:
            for a in anchor_matches:
                if 0 <= a.start() - b.end() <= 60:
                    return MatchResult("PRESENT", snippet_around(folded, b.start(), a.end()),
                                       "TIER0_RIO",
                                       f"Brand + anchor '{' '.join(anchor_toks)}' adjacent")
        if brand_matches and anchor_matches:
            b = brand_matches[0]
            return MatchResult("PARTIAL", snippet_around(folded, b.start(), b.end()),
                               "TIER0_RIO", "Brand and anchor both present but separated",
                               ["NAME_COMPONENTS_SEPARATED"])
        if brand_matches:
            b = brand_matches[0]
            return MatchResult("PARTIAL", snippet_around(folded, b.start(), b.end()),
                               "TIER0_RIO", f"Brand only; anchor '{' '.join(anchor_toks)}' missing",
                               ["BRAND_ONLY"])
        return MatchResult("ABSENT", None, "TIER0_RIO", f"'{name}' not found")

    # ------------------------------------------------------------------ GEO_05
    def check_street(self, text: str) -> MatchResult:
        address = self.school.get("street_address", "")
        if not address:
            return MatchResult("ABSENT", source_tier="TIER0_RIO",
                               provenance="RIO Address 1 empty", flags=["MISSING_GROUND_TRUTH"])
        folded = fold_text(text)

        toks = tokens_of(address)
        house = None
        while toks and re.fullmatch(r"\d+[a-z]?", toks[0]):
            house = toks[0]
            toks = toks[1:]

        lead_dir = toks[0] if toks and toks[0] in DIRECTIONAL_LOOKUP else None
        if lead_dir:
            toks = toks[1:]
        trail_dir = toks[-1] if toks and toks[-1] in DIRECTIONAL_LOOKUP else None
        if trail_dir:
            toks = toks[:-1]
        suffix = toks[-1] if toks and toks[-1] in SUFFIX_LOOKUP else None
        if suffix:
            toks = toks[:-1]
        core = toks
        if not core:
            core, suffix, lead_dir, trail_dir = tokens_of(address), None, None, None
            if core and re.fullmatch(r"\d+[a-z]?", core[0]):
                core = core[1:]
        if not core:
            return MatchResult("ABSENT", source_tier="TIER0_RIO",
                               provenance=f"Could not parse street tokens from '{address}'",
                               flags=["STREET_PARSE_FAILED"])

        core_body = _SEP.join(_body(t, PLACE_EXPANSIONS) for t in core)
        opt_lead = (f"(?:{_body(lead_dir, PLACE_EXPANSIONS)}{_SEP})?" if lead_dir else "")
        opt_trail = (f"(?:{_SEP}{_body(trail_dir, PLACE_EXPANSIONS)})?" if trail_dir else "")

        if suffix:
            suffix_body = "(?:" + "|".join(
                _body(v) for v in sorted(SUFFIX_CLASSES[SUFFIX_LOOKUP[suffix]], key=len, reverse=True)
            ) + ")"
            full_rx = re.compile(
                r"(?<![A-Za-z0-9])" + opt_lead + core_body + _SEP + suffix_body + opt_trail + r"(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            m = full_rx.search(folded)
            if m:
                prov = (f"RIO Address 1 '{address}' — street + suffix class "
                        f"'{SUFFIX_LOOKUP[suffix]}' matched (suffix variants folded)")
                return MatchResult("PRESENT", snippet_around(folded, m.start(), m.end()),
                                   "TIER0_RIO", prov)
            if len(core) >= 2:
                core_rx = re.compile(r"(?<![A-Za-z0-9])" + opt_lead + core_body + r"(?![A-Za-z0-9])",
                                     re.IGNORECASE)
                m = core_rx.search(folded)
                if m:
                    return MatchResult("PARTIAL", snippet_around(folded, m.start(), m.end()),
                                       "TIER0_RIO",
                                       f"Street core '{' '.join(core)}' found without suffix",
                                       ["STREET_CORE_ONLY"])
            # single-word core without its suffix is too generic to credit
            return MatchResult("ABSENT", None, "TIER0_RIO",
                               f"Street '{address}' not found (core-only match not credited for "
                               f"single-word cores)" if len(core) == 1 else
                               f"Street '{address}' not found")

        # ground truth has no recognizable suffix ("Broadway")
        core_rx = re.compile(r"(?<![A-Za-z0-9])" + opt_lead + core_body + opt_trail + r"(?![A-Za-z0-9])",
                             re.IGNORECASE)
        m = core_rx.search(folded)
        if m:
            return MatchResult("PRESENT", snippet_around(folded, m.start(), m.end()),
                               "TIER0_RIO", f"RIO Address 1 '{address}' (suffixless street) matched")
        return MatchResult("ABSENT", None, "TIER0_RIO", f"Street '{address}' not found")


class LocalityResolver:
    """GEO_04 per the ARCHITECTURE §3 decision matrix (Tier-2, gated)."""

    def __init__(self, data_dir: str | Path = "data"):
        d = Path(data_dir)
        self.register: dict[str, dict] = {}
        reg_path = d / "verified_locality_register.csv"
        if reg_path.exists():
            with open(reg_path, newline="", encoding="utf-8") as f:
                self.register = {r["school_id"]: r for r in csv.DictReader(f)}
        self.city_dup: set[str] = set()
        dup_path = d / "locality_city_duplicates.csv"
        if dup_path.exists():
            with open(dup_path, newline="", encoding="utf-8") as f:
                self.city_dup = {r["school_id"] for r in csv.DictReader(f)}
        self.unresolved: dict[str, str] = {}
        un_path = d / "locality_unresolved.csv"
        if un_path.exists():
            with open(un_path, newline="", encoding="utf-8") as f:
                self.unresolved = {r["school_id"]: r["reason"] for r in csv.DictReader(f)}
        self.nearby: dict[str, list[dict]] = {}
        nb_path = d / "nearby_diagnostic_set.json"
        if nb_path.exists():
            self.nearby = json.loads(nb_path.read_text(encoding="utf-8"))

    def _nearby_flags(self, school_id: str, folded: str, exclude_keys: set[str]) -> list[str]:
        flags = []
        for item in self.nearby.get(school_id, []):
            name = item.get("name", "")
            if not name or norm_key(name) in exclude_keys:
                continue
            if phrase_pattern(name, PLACE_EXPANSIONS).search(folded):
                dist = item.get("distance_mi")
                flags.append(f"FAR_LOCALITY_MENTION:{name}" + (f"@{dist}mi" if dist else ""))
        return flags

    def check(self, school_id: str, text: str, school_city: str = "") -> MatchResult:
        school_id = str(school_id)
        folded = fold_text(text)
        exclude = {norm_key(school_city)} if school_city else set()

        row = self.register.get(school_id)
        if row:
            locality = row["locality"]
            variants = [locality] + [a for a in (row.get("locality_aliases") or "").split(";") if a]
            approved = (row.get("approved") or "").strip().lower() == "yes"
            flags = [] if approved else ["PENDING_G1_APPROVAL"]
            exclude.add(norm_key(locality))
            flags += self._nearby_flags(school_id, folded, exclude)
            prov = (f"Verified Locality Register '{locality}' (LOCATED_IN, run "
                    f"{row.get('source_run_date', '?')}; G1 "
                    f"{'approved by ' + row.get('approved_by', '') if approved else 'approval pending'})")
            for v in variants:
                m = phrase_pattern(v, PLACE_EXPANSIONS).search(folded)
                if m:
                    return MatchResult("PRESENT", snippet_around(folded, m.start(), m.end()),
                                       "TIER2_REGISTER", prov, flags)
            return MatchResult("ABSENT", None, "TIER2_REGISTER", prov, flags)

        if school_id in self.city_dup:
            flags = self._nearby_flags(school_id, folded, exclude)
            return MatchResult("N_A", None, "TIER2_REGISTER",
                               "City-fallback cohort: no verified sub-city locality exists; "
                               "GEO_04 excluded from denominators (city scored by GEO_02)", flags)

        reason = self.unresolved.get(school_id, "MISSING")
        flags = [f"UNRESOLVED_LOCALITY_DATA:{reason}"] + self._nearby_flags(school_id, folded, exclude)
        return MatchResult("N_A", None, "TIER2_REGISTER",
                           f"No locality ground truth ({reason}); GEO_04 excluded from denominators",
                           flags)
