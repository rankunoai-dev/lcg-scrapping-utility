"""S7 — scoring engine v2: pure Python math over merged evaluation results.

Changes vs v1 (README Part 5):
  * N_A support — a parameter scored N_A in a scope leaves that scope's
    numerator AND denominator (separate denominators per scope; STR rules
    count toward welcome but never toward page).
  * Location state computed from welcome_status x REST_status (not page),
    which makes WELCOME_ONLY reachable (v1 could never produce it).
  * Weight rule D1 (must_have => 4.0) comes pre-applied in the config.
  * Observation envelope carries config_hash + model/version identifiers;
    a cached observation is only valid when its config_hash matches.
  * Confidence reported = mean of evidence-VERIFIED semantic confidences
    (diagnostic only; never a gate).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ENGINE_VERSION = "2.0.0"

# Bump whenever evaluator/extractor BEHAVIOR changes, so cached observations
# invalidate exactly like a config edit would (observations capture code
# output, not just config input).
PIPELINE_VERSION = "2.1.0"  # welcome scope sourced from client CMS file

STATUS_VALUE = {"PRESENT": 1.0, "PARTIAL": 0.5, "ABSENT": 0.0}

CONFIG_FILES = [
    "config/parameters_database.json",
    "config/brand_defaults.json",
    "config/brand_selectors.json",
    "skills/welcome_seo_evaluator/SKILL.md",
]


def compute_config_hash(root: str | Path = ".") -> str:
    h = hashlib.sha256()
    h.update(PIPELINE_VERSION.encode())
    for rel in CONFIG_FILES:
        p = Path(root) / rel
        if p.exists():
            h.update(rel.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def location_state(welcome_status: str, rest_status: str) -> tuple[str, str]:
    """(location_state, action_category) from the two sub-scopes."""
    w_in = welcome_status in ("PRESENT", "PARTIAL")
    r_in = rest_status in ("PRESENT", "PARTIAL")
    if welcome_status == "N_A" and rest_status == "N_A":
        return "N_A", "NOT_APPLICABLE"
    if rest_status == "N_A":  # welcome-section structural rules
        return "STRUCTURAL", "OPTIMAL" if welcome_status == "PRESENT" else "IMPROVE_STRUCTURE"
    if w_in and r_in:
        return "BOTH", "OPTIMAL"
    if w_in and not r_in:
        return "WELCOME_ONLY", "KEEP_REINFORCE"
    if not w_in and r_in:
        return "WHOLE_PAGE_ONLY", "MOVE_OR_SURFACE"
    return "ABSENT", "CREATE_NEW_CONTENT"


class ScoringEngine:
    def __init__(self, params_config: list[dict],
                 observations_dir: str | Path = "data/observations",
                 config_hash: str | None = None):
        self.params = {p["id"]: p for p in params_config}
        self.observations_dir = Path(observations_dir)
        self.observations_dir.mkdir(parents=True, exist_ok=True)
        self.config_hash = config_hash or compute_config_hash()

    # ----------------------------------------------------------- caching
    def observation_path(self, school_id: str) -> Path:
        return self.observations_dir / f"{school_id}_eval.json"

    def is_observed(self, school_id: str) -> bool:
        """Cache hit only when the stored config_hash matches the current one."""
        p = self.observation_path(school_id)
        if not p.exists():
            return False
        try:
            obs = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        return obs.get("envelope", {}).get("config_hash") == self.config_hash

    def load_observation(self, school_id: str) -> dict:
        return json.loads(self.observation_path(school_id).read_text(encoding="utf-8"))

    # ----------------------------------------------------------- scoring
    def compute_school_scorecard(self, school_id: str, extracted: dict,
                                 results: dict[str, dict],
                                 school_meta: dict | None = None,
                                 llm_meta: dict | None = None) -> dict:
        school_meta = school_meta or {}

        wel_num = wel_den = 0.0
        pg_num = pg_den = 0.0
        mh_num = mh_den = 0.0
        hi_num = hi_den = 0.0
        cat_num: dict[str, float] = {}
        cat_den: dict[str, float] = {}
        confidences: list[float] = []
        flags_rollup: list[str] = []
        parameter_rows: dict[str, dict] = {}

        for pid, cfg in self.params.items():
            weight = float(cfg.get("weight", 1.0))
            res = results.get(pid) or {
                "welcome_status": "ABSENT", "rest_status": "ABSENT",
                "page_status": "ABSENT", "welcome_evidence": None,
                "page_evidence": None, "source_tier": "", "provenance":
                "No evaluation result (defaulted ABSENT)", "flags": ["NO_RESULT"],
                "counts": {}, "method": "NONE",
            }
            ws, rs, ps = res["welcome_status"], res.get("rest_status", "N_A"), res["page_status"]

            if ws != "N_A":
                wel_num += weight * STATUS_VALUE.get(ws, 0.0)
                wel_den += weight
                if cfg.get("must_have"):
                    mh_num += weight * STATUS_VALUE.get(ws, 0.0)
                    mh_den += weight
                if cfg.get("priority") == "High":
                    hi_num += weight * STATUS_VALUE.get(ws, 0.0)
                    hi_den += weight
                cat = cfg.get("category", "GENERAL")
                cat_num[cat] = cat_num.get(cat, 0.0) + weight * STATUS_VALUE.get(ws, 0.0)
                cat_den[cat] = cat_den.get(cat, 0.0) + weight
            if ps != "N_A":
                pg_num += weight * STATUS_VALUE.get(ps, 0.0)
                pg_den += weight

            loc, action = location_state(ws, rs)
            conf = res.get("confidence")
            if conf is not None and "EVIDENCE_UNVERIFIED" not in str(res.get("flags", [])):
                confidences.append(float(conf))
            for f in res.get("flags", []):
                flags_rollup.append(f"{pid}:{f}")

            parameter_rows[pid] = {
                "parameter_name": cfg.get("name", pid),
                "category": cfg.get("category", "GENERAL"),
                "priority": cfg.get("priority", "Medium"),
                "must_have": bool(cfg.get("must_have")),
                "weight": weight,
                "welcome_status": ws,
                "rest_status": rs,
                "page_status": ps,
                "location_state": loc,
                "action_category": action,
                "welcome_evidence": res.get("welcome_evidence"),
                "page_evidence": res.get("page_evidence"),
                "source_tier": res.get("source_tier", ""),
                "provenance": res.get("provenance", ""),
                "flags": res.get("flags", []),
                "counts": res.get("counts", {}),
                "method": res.get("method", ""),
                "confidence": conf,
            }

        def pct(num, den):
            return round(num / den * 100, 2) if den > 0 else 0.0

        s_welcome = pct(wel_num, wel_den)
        s_page = pct(pg_num, pg_den)
        s_must = pct(mh_num, mh_den)
        s_high = pct(hi_num, hi_den)
        s_gap = round(s_page - s_welcome, 2)

        if s_welcome >= 80 and s_must >= 90:
            band = "Tier 1: High Performing"
        elif s_welcome >= 60:
            band = "Tier 2: Medium Optimization Needed"
        else:
            band = "Tier 3: Critical Remediation Priority"

        scorecard = {
            "school_id": str(school_id),
            "school_name": school_meta.get("school_name",
                                           extracted.get("school_name", f"School_{school_id}")),
            "url": school_meta.get("url", ""),
            "brand": school_meta.get("brand", extracted.get("brand", "")),
            "city": school_meta.get("city", ""),
            "state": school_meta.get("state", ""),
            "s_welcome": s_welcome,
            "s_page": s_page,
            "s_must_have": s_must,
            "s_high_priority": s_high,
            "s_gap": s_gap,
            "category_scores_welcome": {c: pct(cat_num[c], cat_den[c]) for c in sorted(cat_num)},
            "mean_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
            "priority_band": band,
            "welcome_found": bool(extracted.get("welcome_found")),
            "welcome_word_count": extracted.get("welcome_word_count", 0),
            "extraction_method": extracted.get("extraction_method", ""),
            "welcome_dom_path": extracted.get("welcome_dom_path", ""),
            "welcome_source": extracted.get("welcome_source", "SCRAPED_FALLBACK"),
            "flags": sorted(flags_rollup
                            + [f"WELCOME:{f}" for f in extracted.get("welcome_source_flags", [])]),
            "parameters": parameter_rows,
            "envelope": {
                "config_hash": self.config_hash,
                "engine_version": ENGINE_VERSION,
                "extractor_version": extracted.get("extractor_version", ""),
                "llm": llm_meta or {},
                "fetch_meta": extracted.get("fetch_meta", {}),
                "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        }
        self.observation_path(school_id).write_text(
            json.dumps(scorecard, indent=1, ensure_ascii=False), encoding="utf-8")
        return scorecard
