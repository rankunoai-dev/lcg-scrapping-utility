"""S6 — semantic evaluator v2: ONE schema-enforced LLM call per school.

Replaces the v1 module that never called an LLM (README Part 5, defect #1).

Trust rules enforced here, not merely requested in the prompt:
  * Evidence-verbatim guard — every PRESENT/PARTIAL verdict must quote a
    string that literally occurs in the correct scope text (whitespace-,
    quote- and case-folded). Unverifiable evidence -> status downgraded to
    ABSENT + flag EVIDENCE_UNVERIFIED. LLM confidence is recorded but never
    gates anything.
  * Landmark guard — GEO_06/07 evidence consisting only of the school's own
    city/state tokens is not a landmark -> downgraded + LANDMARK_GENERIC_GEO.
  * Tier isolation — the LLM receives Tier-0 identity only (name, brand,
    street, city, state). It is never shown locality-register candidates or
    any landmark list, so it cannot echo them back as "findings"
    (ARCHITECTURE §3).
  * Upgrade-only merge — a deterministic ABSENT head keyword may be upgraded
    to PARTIAL by a verified paraphrase; deterministic PRESENT/PARTIAL
    results are never touched.
  * Failure containment — if the call ultimately fails, semantic parameters
    come back N_A + SEMANTIC_FAILED (excluded from denominators, school
    flagged for re-run) instead of fake ABSENTs.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from src.evaluator.deterministic_checker import combine_page_status
from src.evaluator.llm_client import LLMCallMeta, LLMClient, LLMUnavailable
from src.matching.normalize import fold_text, norm_key

logger = logging.getLogger(__name__)

EVALUATOR_VERSION = "2.0.0"
SKILL_PATH = Path("skills/welcome_seo_evaluator/SKILL.md")

SEMANTIC_PARAM_IDS = [
    "GEO_06", "GEO_07",
    "LEAD_01", "LEAD_02", "LEAD_03",
    "QUAL_01", "QUAL_02", "QUAL_03", "QUAL_04",
    "FAC_01", "SAFE_01",
    "INC_01", "INC_02", "INC_03", "INC_04", "INC_05", "INC_06", "INC_07",
    "THM_01", "THM_02", "THM_03", "THM_04", "THM_05", "THM_06", "THM_07", "THM_08",
]
HEAD_KEYWORD_IDS = ["KW_01", "KW_02", "KW_03", "KW_04", "KW_05"]
REST_TEXT_WORD_CAP = 3000  # head+tail sampling beyond this


# ------------------------------------------------------------------ schema
class ScopeEval(BaseModel):
    status: Literal["PRESENT", "PARTIAL", "ABSENT"]
    evidence: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)


class ParamEval(BaseModel):
    param_id: str
    welcome: ScopeEval
    rest: ScopeEval


class SemanticOutput(BaseModel):
    evaluations: list[ParamEval]
    keyword_paraphrases: list[ParamEval] = Field(default_factory=list)


# ------------------------------------------------------------------ helpers
_WS_RX = re.compile(r"\s+")


def _verify_key(s: str) -> str:
    """Folding used for the verbatim containment check."""
    return _WS_RX.sub(" ", fold_text(s)).casefold().strip()


def evidence_in_text(evidence: str | None, scope_text: str) -> bool:
    if not evidence:
        return False
    return _verify_key(evidence) in _verify_key(scope_text)


class SemanticEvaluator:
    def __init__(self, params_config: list[dict], brand_cfg: dict,
                 llm_client: LLMClient | None = None,
                 skill_path: str | Path = SKILL_PATH):
        self.params = {p["id"]: p for p in params_config}
        self.brand_cfg = brand_cfg
        self.client = llm_client or LLMClient()
        self.skill_text = Path(skill_path).read_text(encoding="utf-8")

    # ------------------------------------------------------------ payload
    @staticmethod
    def _cap_rest(rest: str) -> str:
        words = rest.split()
        if len(words) <= REST_TEXT_WORD_CAP:
            return rest
        head = " ".join(words[: int(REST_TEXT_WORD_CAP * 0.7)])
        tail = " ".join(words[-int(REST_TEXT_WORD_CAP * 0.3):])
        return head + "\n[... middle of page truncated ...]\n" + tail

    def build_payload(self, extracted: dict, school: dict,
                      absent_keywords: list[str]) -> str:
        brand = school.get("brand", "")
        display = self.brand_cfg.get("brands", {}).get(brand, {}).get("display_name", brand)
        identity = {
            "school_name": school.get("school_name", ""),
            "brand": display,
            "street_address": school.get("street_address", ""),
            "city": school.get("city", ""),
            "state": school.get("state", ""),
        }
        kw_lines = [
            f"- {kid}: {self.params[kid]['name']}"
            for kid in absent_keywords if kid in self.params
        ]
        return (
            "## School identity (verified ground truth — for anchoring only, "
            "never evidence)\n"
            + json.dumps(identity, ensure_ascii=False)
            + "\n\n## Parameters to evaluate\n"
            + ", ".join(SEMANTIC_PARAM_IDS)
            + ("\n\n## Head keywords deterministically ABSENT — judge paraphrase "
               "intent per the rubric (return under keyword_paraphrases)\n"
               + "\n".join(kw_lines) if kw_lines else
               "\n\n(no head-keyword paraphrase checks needed)")
            + "\n\n## welcome_text\n" + (extracted.get("welcome_text") or "(empty)")
            + "\n\n## rest_text\n" + (self._cap_rest(extracted.get("body_rest_text") or "(empty)"))
        )

    # ------------------------------------------------------------- guards
    def _guard_scope(self, pid: str, scope: ScopeEval, scope_text: str,
                     school: dict) -> tuple[str, str | None, list[str]]:
        """Returns (status, evidence, flags) after verification."""
        flags: list[str] = []
        status, evidence = scope.status, scope.evidence

        if status in ("PRESENT", "PARTIAL"):
            if not evidence_in_text(evidence, scope_text):
                return "ABSENT", None, ["EVIDENCE_UNVERIFIED"]
            if pid in ("GEO_06", "GEO_07"):
                # Whole-token removal — substring replacement would corrupt
                # longer words ("fl" inside "florida") and let generics through.
                from src.matching.normalize import STATES
                geo_words: set[str] = set()
                full_state = STATES.get((school.get("state") or "").upper(), "")
                for phrase in (school.get("city", ""), school.get("state", ""), full_state):
                    geo_words.update(norm_key(phrase).split())
                stop = {"in", "near", "of", "the", "at", "located", "and", "to", "our"}
                residue = [t for t in norm_key(evidence).split()
                           if t not in geo_words and t not in stop]
                if not residue:
                    return "ABSENT", None, ["LANDMARK_GENERIC_GEO"]
        else:
            evidence = None
        return status, evidence, flags

    # ------------------------------------------------------------ evaluate
    def evaluate(self, extracted: dict, school: dict,
                 deterministic_results: dict[str, dict]) -> tuple[dict[str, dict], LLMCallMeta | None]:
        """Returns (results for semantic params + keyword upgrades applied
        in-place on deterministic_results, call meta or None on failure)."""
        welcome_text = extracted.get("welcome_text") or ""
        rest_text = extracted.get("body_rest_text") or ""

        absent_kws = [
            kid for kid in HEAD_KEYWORD_IDS
            if deterministic_results.get(kid, {}).get("welcome_status") == "ABSENT"
            or deterministic_results.get(kid, {}).get("rest_status") == "ABSENT"
        ]
        payload = self.build_payload(extracted, school, absent_kws)

        try:
            parsed, meta = self.client.evaluate(self.skill_text, payload, SemanticOutput)
        except LLMUnavailable as e:
            logger.error("[%s] semantic evaluation failed: %s",
                         school.get("school_id"), e)
            na = {
                pid: {
                    "welcome_status": "N_A", "rest_status": "N_A", "page_status": "N_A",
                    "welcome_evidence": None, "page_evidence": None,
                    "source_tier": "LLM_SEMANTIC",
                    "provenance": f"Semantic evaluation unavailable: {e}",
                    "flags": ["SEMANTIC_FAILED"], "counts": {},
                    "method": "SEMANTIC", "confidence": None,
                } for pid in SEMANTIC_PARAM_IDS
            }
            return na, None

        by_id = {e.param_id: e for e in parsed.evaluations}
        results: dict[str, dict] = {}
        prov = (f"LLM semantic evaluation (model {meta.model}, "
                f"skill v2.0.0, evidence-verified)")

        for pid in SEMANTIC_PARAM_IDS:
            entry = by_id.get(pid)
            if entry is None:
                results[pid] = {
                    "welcome_status": "N_A", "rest_status": "N_A", "page_status": "N_A",
                    "welcome_evidence": None, "page_evidence": None,
                    "source_tier": "LLM_SEMANTIC", "provenance": prov,
                    "flags": ["PARAM_MISSING_FROM_RESPONSE"], "counts": {},
                    "method": "SEMANTIC", "confidence": None,
                }
                continue
            ws, wev, wfl = self._guard_scope(pid, entry.welcome, welcome_text, school)
            rs, rev, rfl = self._guard_scope(pid, entry.rest, rest_text, school)
            results[pid] = {
                "welcome_status": ws, "rest_status": rs,
                "page_status": combine_page_status(ws, rs),
                "welcome_evidence": wev, "page_evidence": rev or wev,
                "source_tier": "LLM_SEMANTIC", "provenance": prov,
                "flags": sorted({*(f"W:{f}" for f in wfl), *(f"R:{f}" for f in rfl)}),
                "counts": {},
                "method": "SEMANTIC",
                "confidence": round((entry.welcome.confidence + entry.rest.confidence) / 2, 3),
            }

        # ---- upgrade-only keyword paraphrases ---------------------------
        for entry in parsed.keyword_paraphrases:
            pid = entry.param_id
            det = deterministic_results.get(pid)
            if det is None or pid not in HEAD_KEYWORD_IDS:
                continue
            for scope_name, scope_eval, text in (
                    ("welcome", entry.welcome, welcome_text),
                    ("rest", entry.rest, rest_text)):
                if det.get(f"{scope_name}_status") != "ABSENT":
                    continue  # upgrade-only: deterministic hits stay untouched
                if scope_eval.status in ("PRESENT", "PARTIAL") and \
                        evidence_in_text(scope_eval.evidence, text):
                    det[f"{scope_name}_status"] = "PARTIAL"
                    key = "welcome_evidence" if scope_name == "welcome" else "page_evidence"
                    det[key] = scope_eval.evidence
                    det.setdefault("flags", []).append(
                        f"SEMANTIC_PARAPHRASE_UPGRADE_{scope_name.upper()}")
            det["page_status"] = combine_page_status(det["welcome_status"],
                                                     det["rest_status"])
            # page scope includes welcome: a welcome-side upgrade must back
            # the page verdict too (validator rule V6)
            if det["page_status"] in ("PRESENT", "PARTIAL") and \
                    not det.get("page_evidence"):
                det["page_evidence"] = det.get("welcome_evidence")
        return results, meta
