"""S8 — report generator v2: 7-sheet Excel deliverable.

Sheets:
  00_Run_Summary        run metadata, cohort counts, fetch/LLM stats, cost
  01_School_Scorecard   one row per school, ranked
  02_Parameter_Presence_Matrix   school x parameter (status, state, evidence)
  03_Parameter_Summary  network-wide coverage per parameter
  04_Remediation_Queue  copywriter actions — THE PRESCRIPTION GATE LIVES HERE
  05_Evaluation_Evidence  per-school extraction/audit metadata
  06_Location_Review    everything a human must look at before trusting/acting

Prescription rules enforced in code (ARCHITECTURE §3, defense layer 2):
  * GEO_04 CREATE actions may name a locality ONLY from an approved
    Verified Locality Register row; pending rows route to 06, never to 04.
  * NEARBY (>5 mi) names never appear anywhere in 04.
  * GEO_06/07 gaps become FIELD TASKS — no landmark text is ever suggested.
  * Tier-0 values (city/state/street/name) may be prescribed freely.

Excel safety: every cell passes _clean() — control chars stripped, 32k
truncation, formula-injection prefix. Matrix sheet skips per-cell styling
(62k rows); headers, freeze panes and autofilter only.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

REPORTER_VERSION = "2.0.0"

_ILLEGAL_RX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t")
MAX_CELL = 32000

# $/MTok (input, output) for cost estimate on 00_Run_Summary
_PRICES = {
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

FIELD_TASK_LANDMARK = ("FIELD TASK: collect 1-2 genuine nearby landmarks from the "
                       "school director. Landmarks are never auto-suggested "
                       "(ARCHITECTURE §3).")


def _clean(value):
    """Excel-safe cell value: no control chars, no formula injection, <=32k."""
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return value
    s = _ILLEGAL_RX.sub(" ", str(value))
    if len(s) > MAX_CELL:
        s = s[:MAX_CELL - 15] + " ...[truncated]"
    if s.startswith(_FORMULA_PREFIXES):
        s = "'" + s
    return s


class ReportGenerator:
    def __init__(self, output_path: str | Path = "LCG_Welcome_SEO_Audit.xlsx",
                 locality_register: dict[str, dict] | None = None,
                 nearby_names: dict[str, list[str]] | None = None):
        self.output_path = Path(output_path)
        self.register = locality_register or {}
        self.nearby = nearby_names or {}

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _sheet(wb, title, headers):
        ws = wb.create_sheet(title=title)
        ws.append([_clean(h) for h in headers])
        fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        for c in ws[1]:
            c.fill = fill
            c.font = font
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.freeze_panes = "A2"
        return ws

    @staticmethod
    def _autosize(ws, max_width=55, sample_rows=60):
        for idx in range(1, ws.max_column + 1):
            letter = get_column_letter(idx)
            width = 10
            for row in ws.iter_rows(min_row=1, max_row=min(sample_rows, ws.max_row),
                                    min_col=idx, max_col=idx):
                v = row[0].value
                if v is not None:
                    width = max(width, min(len(str(v)) + 2, max_width))
            ws.column_dimensions[letter].width = width

    # --------------------------------------------------- prescription gate
    def remediation_action(self, school_id: str, pid: str, pdata: dict) -> tuple[str, str, str] | None:
        """Returns (action_title, action_detail, provenance) for the queue,
        or None when the row belongs on 06_Location_Review instead."""
        cat = pdata.get("action_category")
        name = pdata.get("parameter_name", pid)

        if cat == "MOVE_OR_SURFACE":
            return ("MOVE / SURFACE",
                    f"Copy already exists elsewhere on the page — surface it in the "
                    f"Welcome section. Page evidence: "
                    f"{pdata.get('page_evidence') or '(see matrix sheet)'}",
                    pdata.get("provenance", ""))

        if cat == "IMPROVE_STRUCTURE":
            detail = ("Rewrite Welcome section to 180-300 words."
                      if pid == "STR_01" else
                      "Rework the opening 2 sentences to lead with campus-specific "
                      "local signals (city, street, landmark) instead of generic "
                      "brand copy.")
            return ("IMPROVE STRUCTURE", detail, pdata.get("provenance", ""))

        if cat != "CREATE_NEW_CONTENT":
            return None

        if pid in ("GEO_06", "GEO_07"):
            return ("FIELD TASK", FIELD_TASK_LANDMARK, "Tier-3 policy: landmarks are detect-only")

        if pid == "GEO_04":
            row = self.register.get(str(school_id))
            if row is None:
                return None  # city-dup / unresolved never produce a locality task
            if (row.get("approved") or "").strip().lower() == "yes":
                return ("CREATE NEW CONTENT",
                        f"ADD locality \"{row['locality']}\" to the Welcome section.",
                        f"Verified Locality Register (Google reverse-geocode at school "
                        f"coordinates, run-stable); G1 approved by "
                        f"{row.get('approved_by', '?')} on {row.get('approved_date', '?')}")
            return None  # pending approval -> 06_Location_Review

        return ("CREATE NEW CONTENT",
                f"Draft new Welcome copy covering: {name}.",
                pdata.get("provenance", ""))

    # -------------------------------------------------------------- build
    def generate_report(self, scorecards: list[dict], params_config: list[dict],
                        run_context: dict | None = None) -> Path:
        run_context = run_context or {}
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        cards = sorted(scorecards, key=lambda s: (-s.get("s_welcome", 0),
                                                  -s.get("s_must_have", 0),
                                                  int(s.get("school_id", 0) or 0)))

        # ---- 00_Run_Summary -------------------------------------------
        ws0 = self._sheet(wb, "00_Run_Summary", ["Metric", "Value", "Notes"])
        llm = run_context.get("llm_rollup", {})
        model = llm.get("model", "")
        in_t, out_t = llm.get("input_tokens", 0), llm.get("output_tokens", 0)
        pin, pout = _PRICES.get(model, (0, 0))
        cost = round(in_t / 1e6 * pin + out_t / 1e6 * pout, 2)
        n_sem_failed = sum(1 for s in cards
                           if any("SEMANTIC_FAILED" in f for f in s.get("flags", [])))
        rows0 = [
            ("Report generated (UTC)", datetime.now(timezone.utc).isoformat(timespec="seconds"), ""),
            ("Reporter version", REPORTER_VERSION, ""),
            ("Config hash", run_context.get("config_hash", ""), "cache-invalidation key"),
            ("LLM model", model, "env LCG_LLM_MODEL"),
            ("Schools scored", len(cards), ""),
            ("Welcome section found", sum(1 for s in cards if s.get("welcome_found")),
             "welcome_found=False schools score welcome-scope ABSENT and need review"),
            ("Semantic evaluation failed/skipped", n_sem_failed,
             "semantic params N_A; re-run with --eval-only"),
            ("GEO_04 register schools", sum(1 for s in cards
                                            if str(s.get("school_id")) in self.register), ""),
            ("GEO_04 register rows pending G1 approval",
             sum(1 for r in self.register.values()
                 if (r.get("approved") or "").strip().lower() != "yes"),
             "pending rows never reach 04_Remediation_Queue"),
            ("Fetch manifest", str(run_context.get("manifest_summary", {})), ""),
            ("LLM tokens (in/out)", f"{in_t} / {out_t}", ""),
            ("Estimated LLM cost (USD)", cost, "first-party API prices"),
            ("Snapshot window", run_context.get("snapshot_window", ""), "fetch dates"),
        ]
        for r in rows0:
            ws0.append([_clean(x) for x in r])
        self._autosize(ws0, max_width=80)

        # ---- 01_School_Scorecard --------------------------------------
        ws1 = self._sheet(wb, "01_School_Scorecard", [
            "Rank", "School ID", "School Name", "Brand", "City", "State",
            "Welcome Score", "Whole Page Score", "Placement Gap",
            "Must-Have Score", "High Priority Score", "Priority Band",
            "Welcome Found", "Welcome Words", "Mean Confidence"])
        for i, s in enumerate(cards, 1):
            ws1.append([_clean(x) for x in (
                i, s.get("school_id"), s.get("school_name"), s.get("brand"),
                s.get("city"), s.get("state"), s.get("s_welcome"), s.get("s_page"),
                s.get("s_gap"), s.get("s_must_have"), s.get("s_high_priority"),
                s.get("priority_band"), s.get("welcome_found"),
                s.get("welcome_word_count"), s.get("mean_confidence"))])
        ws1.auto_filter.ref = ws1.dimensions
        self._autosize(ws1)

        # ---- 02_Parameter_Presence_Matrix ------------------------------
        ws2 = self._sheet(wb, "02_Parameter_Presence_Matrix", [
            "School ID", "School Name", "Parameter ID", "Parameter Name",
            "Category", "Priority", "Must Have", "Welcome Status", "Rest Status",
            "Page Status", "Location State", "Action Category",
            "Welcome Evidence", "Page Evidence", "Source Tier", "Flags"])
        for s in cards:
            for pid, p in s.get("parameters", {}).items():
                ws2.append([_clean(x) for x in (
                    s.get("school_id"), s.get("school_name"), pid,
                    p.get("parameter_name"), p.get("category"), p.get("priority"),
                    "Yes" if p.get("must_have") else "No", p.get("welcome_status"),
                    p.get("rest_status"), p.get("page_status"),
                    p.get("location_state"), p.get("action_category"),
                    p.get("welcome_evidence"), p.get("page_evidence"),
                    p.get("source_tier"), ";".join(p.get("flags", [])))])
        ws2.auto_filter.ref = ws2.dimensions
        self._autosize(ws2)

        # ---- 03_Parameter_Summary --------------------------------------
        ws3 = self._sheet(wb, "03_Parameter_Summary", [
            "Parameter ID", "Parameter Name", "Category", "Priority", "Must Have",
            "Welcome Coverage %", "Page Coverage %", "N_A Count",
            "OPTIMAL", "MOVE", "KEEP", "CREATE"])
        n = len(cards) or 1
        for cfg in params_config:
            pid = cfg["id"]
            wel = pg = na = 0
            acts = {"OPTIMAL": 0, "MOVE_OR_SURFACE": 0, "KEEP_REINFORCE": 0,
                    "CREATE_NEW_CONTENT": 0}
            for s in cards:
                p = s.get("parameters", {}).get(pid, {})
                ws_, ps_ = p.get("welcome_status"), p.get("page_status")
                if ws_ == "N_A":
                    na += 1
                elif ws_ in ("PRESENT", "PARTIAL"):
                    wel += 1
                if ps_ in ("PRESENT", "PARTIAL"):
                    pg += 1
                if p.get("action_category") in acts:
                    acts[p["action_category"]] += 1
            applicable = n - na or 1
            ws3.append([_clean(x) for x in (
                pid, cfg.get("name"), cfg.get("category"), cfg.get("priority"),
                "Yes" if cfg.get("must_have") else "No",
                round(wel / applicable * 100, 1), round(pg / applicable * 100, 1),
                na, acts["OPTIMAL"], acts["MOVE_OR_SURFACE"],
                acts["KEEP_REINFORCE"], acts["CREATE_NEW_CONTENT"])])
        ws3.auto_filter.ref = ws3.dimensions
        self._autosize(ws3)

        # ---- 04_Remediation_Queue (prescription-gated) ------------------
        ws4 = self._sheet(wb, "04_Remediation_Queue", [
            "School ID", "School Name", "Priority Band", "Parameter ID",
            "Parameter Name", "Priority", "Action", "Action Detail", "Provenance"])
        for s in cards:
            for pid, p in s.get("parameters", {}).items():
                act = self.remediation_action(str(s.get("school_id")), pid, p)
                if act is None:
                    continue
                title, detail, prov = act
                ws4.append([_clean(x) for x in (
                    s.get("school_id"), s.get("school_name"), s.get("priority_band"),
                    pid, p.get("parameter_name"), p.get("priority"),
                    title, detail, prov)])
        ws4.auto_filter.ref = ws4.dimensions
        self._autosize(ws4, max_width=90)

        # ---- 05_Evaluation_Evidence -------------------------------------
        ws5 = self._sheet(wb, "05_Evaluation_Evidence", [
            "School ID", "School Name", "Welcome Found", "Extraction Method",
            "Welcome DOM Path", "Welcome Words", "Fetch Timestamp", "Final URL",
            "LLM Model", "Config Hash", "Mean Confidence", "School Flags"])
        for s in cards:
            env = s.get("envelope", {})
            fm = env.get("fetch_meta", {})
            ws5.append([_clean(x) for x in (
                s.get("school_id"), s.get("school_name"), s.get("welcome_found"),
                s.get("extraction_method"), s.get("welcome_dom_path"),
                s.get("welcome_word_count"), fm.get("fetch_ts"), fm.get("final_url"),
                env.get("llm", {}).get("model", ""), env.get("config_hash"),
                s.get("mean_confidence"), "; ".join(s.get("flags", [])[:40]))])
        ws5.auto_filter.ref = ws5.dimensions
        self._autosize(ws5, max_width=70)

        # ---- 06_Location_Review ------------------------------------------
        ws6 = self._sheet(wb, "06_Location_Review", [
            "School ID", "School Name", "Issue Type", "Parameter", "Detail",
            "Requires"])
        review_flags = ("FAR_LOCALITY_MENTION", "UNRESOLVED_LOCALITY_DATA",
                        "EVIDENCE_UNVERIFIED", "LANDMARK_GENERIC_GEO",
                        "SEMANTIC_FAILED", "PENDING_G1_APPROVAL")
        for s in cards:
            sid = str(s.get("school_id"))
            if not s.get("welcome_found"):
                ws6.append([_clean(x) for x in (
                    sid, s.get("school_name"), "NO_WELCOME_SECTION", "-",
                    "No Welcome section found; welcome scope scored ABSENT",
                    "Analyst review")])
            for pid, p in s.get("parameters", {}).items():
                for f in p.get("flags", []):
                    base = f.split(":")[-1] if f.startswith(("W:", "R:")) else f
                    if any(base.startswith(rf) for rf in review_flags):
                        detail = f
                        if base.startswith("PENDING_G1_APPROVAL"):
                            row = self.register.get(sid, {})
                            detail = (f"Register locality \"{row.get('locality', '?')}\" "
                                      f"pending G1 approval; current page status: "
                                      f"{p.get('welcome_status')}")
                        ws6.append([_clean(x) for x in (
                            sid, s.get("school_name"), base.split(":")[0], pid,
                            detail, "Analyst review")])
        for extra in run_context.get("ingest_review_rows", []):
            ws6.append([_clean(x) for x in (
                extra.get("school_id"), extra.get("school_name", ""),
                extra.get("issue", "INGEST_FLAG"), "-", extra.get("detail", ""),
                "Client data fix")])
        ws6.auto_filter.ref = ws6.dimensions
        self._autosize(ws6, max_width=90)

        wb.save(self.output_path)
        return self.output_path
