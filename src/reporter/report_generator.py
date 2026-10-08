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
from openpyxl.comments import Comment
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
    "anthropic/claude-opus-5.5": (4.0, 20.0),
    "anthropic/claude-sonnet-5.5": (2.0, 10.0),
    "anthropic/claude-haiku-4.5": (1.0, 5.0),
}

FIELD_TASK_LANDMARK = ("FIELD TASK: collect 1-2 genuine nearby landmarks from the "
                       "school director. Landmarks are never auto-suggested "
                       "(ARCHITECTURE §3).")

PIVOT_EVIDENCE_CHARS = 140  # quoted page text shown inside pivot cells

# Status cell fills for the presence pivots (light, print-friendly)
_STATUS_FILLS = {
    "PRESENT": PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
    "PARTIAL": PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"),
    "ABSENT": PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
    "N_A": PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid"),
}

# Worked examples for the glossary (school 7440, verified live). Note the
# canonical IDs: City and State are separate parameters, Street Address is
# GEO_05, and intersections / cross-streets count as LANDMARK evidence under
# GEO_06/07 rather than as a parameter of their own.
PARAM_EXAMPLES = {
    "GEO_01": 'La Petite Academy on Beach Blvd.',
    "GEO_02": 'Jacksonville',
    "GEO_03": 'FL / Florida (guarded: "FL" only counts next to a comma, city or ZIP)',
    "GEO_04": 'Golden Glades / The Woods (sub-city neighborhood from the Verified Locality Register)',
    "GEO_05": '3680 Beach Blvd — "Beach Blvd" and "Beach Boulevard" both count',
    "GEO_06": "across from Denny's — or an intersection like \"between Hodges Blvd and San Pablo Rd\"",
    "GEO_07": 'a SECOND distinct landmark or intersection on the same page',
    "LEAD_01": 'Dee Pryor, Director',
    "LEAD_02": '15 years in early education',
    "LEAD_03": 'Since 1991 / serving Jacksonville families for 30 years',
    "QUAL_01": 'NECPA accredited; Gold Seal Quality Care',
    "QUAL_02": 'CPR and First Aid certified staff',
    "QUAL_03": 'Voted Best Preschool 2025',
    "QUAL_04": 'a combined 100 years of experience',
    "FAC_01": 'STEAM studio; shaded playground',
    "TECH_01": 'SproutAbout (Montessori Compass for Montessori Unlimited schools)',
    "SAFE_01": 'keypad entry; secure check-in',
    "STR_01": 'Welcome section is 180-300 words',
    "STR_02": 'the first 2 sentences name the city, street or a landmark',
}

# ---------------------------------------------------------------------------
# Column meanings — ONE source of truth used twice: (a) a hover tooltip on
# every header cell of every sheet, (b) the COLUMN DICTIONARY section at the
# bottom of 00_Run_Summary. Columns shared by several sheets live in
# COMMON_COLUMN_TIPS; sheet-specific definitions override them.
# ---------------------------------------------------------------------------
COMMON_COLUMN_TIPS = {
    "School ID": "LCG school number from the client's RIO location export (ground truth).",
    "School Name": "Location Name from the RIO export.",
    "School URL": "The school page that was fetched and audited.",
    "Brand": "LCG brand this school belongs to (RIO export).",
    "City": "School's city (RIO export).",
    "State": "School's state (RIO export).",
    "Parameter ID": "Canonical parameter code (see the PARAMETER GLOSSARY on sheet 00).",
    "Parameter Name": "Human-readable parameter name.",
    "Category": "Parameter group: GEO, KEYWORDS_HEAD, SEMANTIC_KEYWORDS, ALL_INCLUSIVE, THEMES, STRUCTURE.",
    "Priority": "Scoring priority: must-have weight 4.0, High 3.0, Medium 1.5, Low 1.0.",
    "Must Have": "Yes = one of the 11 parameters every Welcome section must contain.",
    "Welcome Found": "FALSE = no Welcome text from the client CMS file or the live page; welcome scope scores ABSENT and the school needs review.",
    "Welcome Words": "Word count of the Welcome text (target 180-300).",
    "Mean Confidence": "Average AI self-confidence across evidence-VERIFIED semantic verdicts. Diagnostic only - never used in scores.",
    "Priority Band": "Tier 1 = Welcome>=80 and Must-Have>=90; Tier 2 = Welcome>=60; Tier 3 = below 60 (most urgent).",
    "Metric": "Name of the run-level statistic.",
    "Value": "The statistic's value for this run.",
    "Notes": "How to interpret the metric.",
}

HEADER_TOOLTIPS = {
    "01_School_Scorecard": {
        "Rank": "Score-based rank (1 = best Welcome Score). Row ORDER follows the client's dropped-pages batch sequencing when one is loaded.",
        "Client Batch": "Batch number from the client's dropped-pages file (blank = school not in that file; those rows follow after the batched ones).",
        "Welcome Score": "Weighted % of all 56 parameters present in the WELCOME SECTION only (PARTIAL counts half; N_A parameters leave the denominator).",
        "Whole Page Score": "Same weighted % measured across the whole page (Welcome included).",
        "Placement Gap": "Whole Page Score minus Welcome Score. Positive = content exists on the page that the Welcome fails to surface.",
        "Must-Have Score": "Welcome-section coverage of the 11 must-have parameters (full list in this header's richer tooltip).",
        "High Priority Score": "Welcome-section coverage of the High-priority parameters (full list in this header's richer tooltip).",
    },
    "02_Parameter_Presence_Matrix": {
        "Welcome Status": "PRESENT / PARTIAL / ABSENT in the Welcome text only. N_A = parameter not applicable to this school.",
        "Rest Status": "Status in the page content EXCLUDING the Welcome section (nav/footer also excluded).",
        "Page Status": "Combined whole-page status (best of Welcome and Rest).",
        "Location State": "BOTH / WELCOME_ONLY / WHOLE_PAGE_ONLY / ABSENT from Welcome x Rest; STRUCTURAL = welcome-section rule.",
        "Action Category": "OPTIMAL keep; KEEP_REINFORCE welcome-only; MOVE_OR_SURFACE exists on page - surface it in the Welcome; CREATE_NEW_CONTENT missing everywhere; IMPROVE_STRUCTURE structural fix; NOT_APPLICABLE excluded.",
        "Welcome Evidence": "VERBATIM text from the Welcome that justifies the status. AI quotes are verified to literally exist, or the verdict is downgraded.",
        "Page Evidence": "VERBATIM text from the page (rest scope; falls back to the Welcome quote when page presence derives from the Welcome).",
        "Source Tier": "Ground-truth origin: TIER0_RIO client export; TIER2_REGISTER verified locality register; DETERMINISTIC_KEYWORD regex engine; LLM_SEMANTIC AI pass; TIER0_BRAND_RULE brand app rule; STRUCTURAL welcome rule.",
        "Flags": "Machine flags: EVIDENCE_UNVERIFIED (AI quote not found - downgraded), PENDING_G1_APPROVAL (locality awaits analyst sign-off), SEMANTIC_PARAPHRASE_UPGRADE (keyword matched by meaning), FAR_LOCALITY_MENTION (area >5 miles away - no credit).",
    },
    "03_Parameter_Summary": {
        "Welcome Coverage %": "Share of applicable schools (N_A excluded) where the parameter is PRESENT or PARTIAL in the Welcome.",
        "Page Coverage %": "Share of applicable schools where it is PRESENT or PARTIAL anywhere on the page.",
        "N_A Count": "Schools where this parameter does not apply (excluded from the percentages).",
        "OPTIMAL": "Schools where the parameter is covered in the Welcome AND elsewhere.",
        "MOVE": "Schools where it exists on the page but not in the Welcome - cheapest fix: surface the existing copy.",
        "KEEP": "Schools where it lives only in the Welcome - keep it, consider echoing elsewhere.",
        "CREATE": "Schools where it is missing everywhere - new copy needed.",
    },
    "04_Remediation_Queue": {
        "Action": "MOVE / SURFACE lift existing page copy into the Welcome; CREATE NEW CONTENT write new copy; IMPROVE STRUCTURE fix word count or local-first opening; FIELD TASK information must come from the school director (never invented).",
        "Action Detail": "Exact instruction for the copywriter, including the page evidence to reuse where it exists.",
        "Provenance": "Why the instruction can be trusted (data source; for localities, who approved the name and when).",
    },
    "05_Evaluation_Evidence": {
        "Snapshot Date": "The day the live page was fetched; the audit reflects the page as of this date.",
        "AI Claims Rejected": "AI verdicts discarded because the quoted evidence was not found verbatim (details in sheet 02).",
        "Keyword Wording Opportunities": "Head keywords the page covers by MEANING but never says exactly - quick copy win: the quoted sentence already exists, add the exact search term. Counted as PARTIAL until then.",
        "Review Notes": "Plain-English summary of anything a human should check for this school; 06_Location_Review holds the actionable queue.",
    },
    "06_Location_Review": {
        "Issue Type": "NO_WELCOME_SECTION; FAR_LOCALITY_MENTION (neighborhood >5 miles away - no credit); UNRESOLVED_LOCALITY_DATA; EVIDENCE_UNVERIFIED; LANDMARK_GENERIC_GEO (city/state offered as a landmark); PENDING_G1_APPROVAL; SEMANTIC_FAILED; INGEST_FLAG (client data error).",
        "Parameter": "Parameter the issue belongs to (hover the cell for its name); '-' = school-level issue.",
        "Detail": "What exactly was found, with names and distances where relevant.",
        "Requires": "Who must act: Analyst review, or Client data fix.",
    },
    "07_Welcome_Presence_Pivot": {
        "(parameter columns)": "One column per parameter; cell = status in the WELCOME text only, with the verbatim quote embedded for PRESENT/PARTIAL. Green PRESENT, yellow PARTIAL, red ABSENT, grey N_A.",
    },
    "08_Page_Excl_Welcome_Pivot": {
        "(parameter columns)": "Same layout, measured on the page EXCLUDING the Welcome section. A parameter present only in the Welcome shows ABSENT here - comparing the two sheets reveals MOVE vs CREATE.",
    },
    "09_Welcome_Present_Absent": {
        "Welcome Source": "CLIENT_FILE = Welcome text came from the client CMS export (authoritative); SCRAPED_FALLBACK = school missing from the file, live page used; NONE = no Welcome text from either source.",
        "Present": "Every parameter fully PRESENT in the Welcome text, listed by name.",
        "Partial": "Parameters only PARTIALLY satisfied in the Welcome (e.g. keyword matched by meaning, brand-only name).",
        "Absent": "Every parameter ABSENT from the Welcome text. Compare with the next column: absent here but NOT there = it exists elsewhere on the page (MOVE/SURFACE - cheapest fix).",
        "Absent on Whole Page": "Parameters missing from the ENTIRE school page (Welcome + everything else; nav/footer excluded) - the true CREATE-new-content list. Always a subset of the Absent column. N_A parameters are excluded from all lists.",
    },
}


def _header_tip(sheet_title: str, column: str) -> str | None:
    tips = HEADER_TOOLTIPS.get(sheet_title, {})
    return tips.get(column) or COMMON_COLUMN_TIPS.get(column)


COLUMN_DICTIONARY = (
    [('(all sheets)', col, tip) for col, tip in COMMON_COLUMN_TIPS.items()
     if col not in ('Metric', 'Value', 'Notes')]
    + [(sheet, col, tip)
       for sheet in sorted(HEADER_TOOLTIPS)
       for col, tip in HEADER_TOOLTIPS[sheet].items()]
)


def _bullets(items):
    """One bullet per line for wrap_text cells; '(none)' when empty."""
    return "\n".join(f"• {item}" for item in items) or "(none)"


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
                 nearby_names: dict[str, list[str]] | None = None,
                 school_urls: dict[str, str] | None = None,
                 school_order: list[str] | None = None,
                 batch_map: dict[str, str] | None = None):
        self.output_path = Path(output_path)
        self.register = locality_register or {}
        self.nearby = nearby_names or {}
        self.school_urls = school_urls or {}
        # client-supplied row sequencing (e.g. dropped-pages batches); schools
        # outside the sequence follow after, ranked by score
        self.school_order = {sid: i for i, sid in enumerate(school_order or [])}
        self.batch_map = batch_map or {}

    def _url(self, card: dict) -> str:
        return card.get("url") or self.school_urls.get(str(card.get("school_id")), "")

    @staticmethod
    def _param_tooltip(cell, pid: str, params_by_id: dict):
        """Hover comment: parameter name + what it checks (+ example)."""
        cfg = params_by_id.get(pid)
        if cfg is None:
            return
        text = f"{pid} — {cfg.get('name', '')}\n\n{cfg.get('description', '')}"
        example = PARAM_EXAMPLES.get(pid)
        if example:
            text += f"\n\nExample: {example}"
        c = Comment(text[:700], "lcg-utility")
        c.width = 320
        c.height = 160
        cell.comment = c

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
            # every header carries its meaning as a hover tooltip (richer
            # tooltips set later - score lists, parameter glossaries - override)
            tip = _header_tip(title, str(c.value))
            if tip:
                cm = Comment(f"{c.value}\n\n{tip}"[:900], "lcg-utility")
                cm.width = 320
                cm.height = 170
                c.comment = cm
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
        params_by_id = {p["id"]: p for p in params_config}
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        cards = sorted(scorecards, key=lambda s: (-s.get("s_welcome", 0),
                                                  -s.get("s_must_have", 0),
                                                  int(s.get("school_id", 0) or 0)))
        score_rank = {str(s.get("school_id")): i for i, s in enumerate(cards, 1)}
        if self.school_order:
            far = len(self.school_order) + 10_000
            cards = sorted(cards, key=lambda s: (
                self.school_order.get(str(s.get("school_id")), far),
                -s.get("s_welcome", 0)))

        # ---- 00_Run_Summary -------------------------------------------
        ws0 = self._sheet(wb, "00_Run_Summary", ["Metric", "Value", "Notes"])
        llm = run_context.get("llm_rollup", {})
        model = llm.get("model", "")
        in_t, out_t = llm.get("input_tokens", 0), llm.get("output_tokens", 0)
        if "cost_usd" in llm:  # measured by the client's cost cap — authoritative
            cost = round(llm["cost_usd"], 4)
        else:
            pin, pout = _PRICES.get(model, (0, 0))
            cost = round(in_t / 1e6 * pin + out_t / 1e6 * pout, 4)
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
            ("Welcome text source",
             f"{sum(1 for s in cards if s.get('welcome_source') == 'CLIENT_FILE')} from "
             f"client CMS file / "
             f"{sum(1 for s in cards if s.get('welcome_source') == 'SCRAPED_FALLBACK')} "
             f"scraped fallback / "
             f"{sum(1 for s in cards if s.get('welcome_source') not in ('CLIENT_FILE', 'SCRAPED_FALLBACK'))} none",
             "client CMS export is authoritative for the Welcome scope"),
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

        # ---- parameter glossary (what each of the 56 parameters means) ---
        ws0.append(["", "", ""])
        g_hdr = ws0.cell(row=ws0.max_row + 1, column=1,
                         value="PARAMETER GLOSSARY — the 56 parameters this audit checks, with examples")
        g_hdr.font = Font(name="Calibri", size=11, bold=True)
        ws0.append(["Parameter", "What it checks", "Example"])
        for c in ws0[ws0.max_row]:
            c.font = Font(name="Calibri", bold=True)
        for p in params_config:
            label = f"{p['id']} — {p.get('name', '')}"
            desc = p.get("description", "")
            if not desc and p.get("variants"):
                desc = "Keyword presence (exact wording or listed variants)"
            example = PARAM_EXAMPLES.get(p["id"]) or ", ".join(p.get("variants", [])[:4]) \
                or p.get("name", "")
            ws0.append([_clean(label), _clean(desc), _clean(example)])

        # ---- column dictionary (what every column in every sheet means) --
        ws0.append(["", "", ""])
        hdr_cell = ws0.cell(row=ws0.max_row + 1, column=1,
                            value="COLUMN DICTIONARY — meaning of every column in every sheet")
        hdr_cell.font = Font(name="Calibri", size=11, bold=True)
        ws0.append(["Sheet", "Column", "Meaning"])
        for c in ws0[ws0.max_row]:
            c.font = Font(name="Calibri", bold=True)
        for sheet_name, column, meaning in COLUMN_DICTIONARY:
            ws0.append([_clean(sheet_name), _clean(column), _clean(meaning)])
        self._autosize(ws0, max_width=110, sample_rows=ws0.max_row)

        # ---- 01_School_Scorecard --------------------------------------
        ws1 = self._sheet(wb, "01_School_Scorecard", [
            "Rank", "Client Batch", "School ID", "School Name", "School URL",
            "Brand", "City", "State",
            "Welcome Score", "Whole Page Score", "Placement Gap",
            "Must-Have Score", "High Priority Score", "Priority Band",
            "Welcome Found", "Welcome Words", "Mean Confidence"])

        # hover tooltips on the score headers: which parameters are counted
        def _plist(pred):
            return "\n".join(f"  {p['id']} — {p.get('name', '')}"
                             for p in params_config if pred(p))
        must_list = _plist(lambda p: p.get("must_have"))
        high_list = _plist(lambda p: p.get("priority") == "High")
        header_tips = {
            "Welcome Score": "Weighted coverage of ALL 56 parameters measured in the "
                             "Welcome section only.\nWeights: must-have 4.0, high 3.0, "
                             "medium 1.5. PARTIAL counts half; N_A parameters are "
                             "excluded from the denominator.",
            "Whole Page Score": "Same weighted coverage measured across the whole page "
                                "(Welcome section included).",
            "Placement Gap": "Whole Page Score minus Welcome Score.\nPositive = content "
                             "already on the page that the Welcome section fails to "
                             "surface (see MOVE/SURFACE rows in 04_Remediation_Queue).",
            "Must-Have Score": f"Welcome-section coverage of the "
                               f"{must_list.count(chr(10)) + 1} MUST-HAVE parameters "
                               f"(weight 4.0 each):\n{must_list}",
            "High Priority Score": f"Welcome-section coverage of the "
                                   f"{high_list.count(chr(10)) + 1} HIGH-priority "
                                   f"parameters:\n{high_list}",
        }
        for cell in ws1[1]:
            tip = header_tips.get(str(cell.value))
            if tip:
                c = Comment(tip[:1800], "lcg-utility")
                c.width = 340
                c.height = 420 if cell.value in ("Must-Have Score", "High Priority Score") else 150
                cell.comment = c
        for s in cards:
            sid = str(s.get("school_id"))
            ws1.append([_clean(x) for x in (
                score_rank.get(sid, ""), self.batch_map.get(sid, ""),
                sid, s.get("school_name"), self._url(s),
                s.get("brand"), s.get("city"), s.get("state"),
                s.get("s_welcome"), s.get("s_page"),
                s.get("s_gap"), s.get("s_must_have"), s.get("s_high_priority"),
                s.get("priority_band"), s.get("welcome_found"),
                s.get("welcome_word_count"), s.get("mean_confidence"))])
        ws1.auto_filter.ref = ws1.dimensions
        self._autosize(ws1)

        # ---- 02_Parameter_Presence_Matrix ------------------------------
        ws2 = self._sheet(wb, "02_Parameter_Presence_Matrix", [
            "School ID", "School Name", "School URL", "Parameter ID", "Parameter Name",
            "Category", "Priority", "Must Have", "Welcome Status", "Rest Status",
            "Page Status", "Location State", "Action Category",
            "Welcome Evidence", "Page Evidence", "Source Tier", "Flags"])
        for s in cards:
            for pid, p in s.get("parameters", {}).items():
                ws2.append([_clean(x) for x in (
                    s.get("school_id"), s.get("school_name"), self._url(s), pid,
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
            "School ID", "School Name", "School URL", "Priority Band", "Parameter ID",
            "Parameter Name", "Priority", "Action", "Action Detail", "Provenance"])
        for s in cards:
            for pid, p in s.get("parameters", {}).items():
                act = self.remediation_action(str(s.get("school_id")), pid, p)
                if act is None:
                    continue
                title, detail, prov = act
                ws4.append([_clean(x) for x in (
                    s.get("school_id"), s.get("school_name"), self._url(s),
                    s.get("priority_band"),
                    pid, p.get("parameter_name"), p.get("priority"),
                    title, detail, prov)])
        ws4.auto_filter.ref = ws4.dimensions
        self._autosize(ws4, max_width=90)

        # ---- 05_Evaluation_Evidence -------------------------------------
        ws5 = self._sheet(wb, "05_Evaluation_Evidence", [
            "School ID", "School Name", "School URL", "Snapshot Date",
            "Welcome Found", "Welcome Words", "Mean Confidence",
            "AI Claims Rejected", "Keyword Wording Opportunities", "Review Notes"])
        for s in cards:
            fm = s.get("envelope", {}).get("fetch_meta", {})
            rejected, kw_opps, notes = self._review_summary(s)
            ws5.append([_clean(x) for x in (
                s.get("school_id"), s.get("school_name"), self._url(s),
                str(fm.get("fetch_ts", ""))[:10], s.get("welcome_found"),
                s.get("welcome_word_count"), s.get("mean_confidence"),
                rejected, kw_opps or "None — all matched keywords use exact wording",
                notes)])
        ws5.auto_filter.ref = ws5.dimensions
        self._autosize(ws5, max_width=95)

        # ---- 06_Location_Review ------------------------------------------
        ws6 = self._sheet(wb, "06_Location_Review", [
            "School ID", "School Name", "School URL", "Issue Type", "Parameter", "Detail",
            "Requires"])
        review_flags = ("FAR_LOCALITY_MENTION", "UNRESOLVED_LOCALITY_DATA",
                        "EVIDENCE_UNVERIFIED", "LANDMARK_GENERIC_GEO",
                        "SEMANTIC_FAILED", "PENDING_G1_APPROVAL")
        for s in cards:
            sid = str(s.get("school_id"))
            if not s.get("welcome_found"):
                ws6.append([_clean(x) for x in (
                    sid, s.get("school_name"), self._url(s), "NO_WELCOME_SECTION", "-",
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
                            sid, s.get("school_name"), self._url(s),
                            base.split(":")[0], pid,
                            detail, "Analyst review")])
                        self._param_tooltip(ws6.cell(row=ws6.max_row, column=5),
                                            pid, params_by_id)
        for extra in run_context.get("ingest_review_rows", []):
            ws6.append([_clean(x) for x in (
                extra.get("school_id"), extra.get("school_name", ""),
                self.school_urls.get(str(extra.get("school_id")), ""),
                extra.get("issue", "INGEST_FLAG"), "-", extra.get("detail", ""),
                "Client data fix")])
        ws6.auto_filter.ref = ws6.dimensions
        self._autosize(ws6, max_width=90)

        # ---- 07/08: presence pivots (one row per school, one col per param)
        self._presence_pivot(wb, "07_Welcome_Presence_Pivot", cards, params_config,
                             status_key="welcome_status", evidence_key="welcome_evidence")
        self._presence_pivot(wb, "08_Page_Excl_Welcome_Pivot", cards, params_config,
                             status_key="rest_status", evidence_key="page_evidence")

        # ---- 09: per-school present/absent lists (Welcome scope,
        #          client CMS text where available)
        ws9 = self._sheet(wb, "09_Welcome_Present_Absent", [
            "School ID", "School Name", "School URL", "Welcome Source",
            "Present", "Partial", "Absent", "Absent on Whole Page"])
        for s in cards:
            buckets = {"PRESENT": [], "PARTIAL": [], "ABSENT": []}
            page_absent = []
            for pid, p in s.get("parameters", {}).items():
                name = p.get("parameter_name") or pid
                st = p.get("welcome_status")
                if st in buckets:
                    buckets[st].append(name)
                if p.get("page_status") == "ABSENT":
                    page_absent.append(name)
            ws9.append([_clean(x) for x in (
                s.get("school_id"), s.get("school_name"), self._url(s),
                s.get("welcome_source", ""),
                _bullets(buckets["PRESENT"]),
                _bullets(buckets["PARTIAL"]),
                _bullets(buckets["ABSENT"]),
                _bullets(page_absent))])
        for row in ws9.iter_rows(min_row=2, min_col=5, max_col=8):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        ws9.auto_filter.ref = ws9.dimensions
        ws9.column_dimensions["A"].width = 10
        ws9.column_dimensions["B"].width = 32
        ws9.column_dimensions["C"].width = 44
        ws9.column_dimensions["D"].width = 16
        for letter in ("E", "F", "G", "H"):
            ws9.column_dimensions[letter].width = 62

        wb.save(self.output_path)
        return self.output_path

    # ------------------------------------------------- analyst review notes
    def _keyword_opportunities(self, card: dict) -> str:
        """Head keywords the page covers by MEANING but never says exactly —
        a direct copywriting win: add the exact search term, the topic is
        already on the page. Quotes what the page says instead."""
        out = []
        for pid, p in card.get("parameters", {}).items():
            if not pid.startswith("KW_"):
                continue
            kw_flags = [f for f in p.get("flags", []) if "SEMANTIC_PARAPHRASE_UPGRADE" in f]
            if not kw_flags:
                continue
            scopes = []
            if any(f.endswith("WELCOME") for f in kw_flags):
                scopes.append("Welcome")
            if any(f.endswith("REST") for f in kw_flags):
                scopes.append("rest of page")
            ev = p.get("welcome_evidence") or p.get("page_evidence") or ""
            if len(ev) > 90:
                ev = ev[:90] + "..."
            out.append(f"'{p.get('parameter_name', pid)}' never appears exactly "
                       f"({' + '.join(scopes)}) — the page says \"{ev}\" instead; "
                       f"add the exact keyword")
        return " | ".join(out)

    def _review_summary(self, card: dict) -> tuple[int, str, str]:
        """Translate machine flags into (rejected_count, keyword opportunities,
        plain-English notes). Full machine detail stays in sheets 02/06."""
        flags = card.get("flags", [])
        sid = str(card.get("school_id"))
        rejected = sum(1 for f in flags if "EVIDENCE_UNVERIFIED" in f)
        kw_opps = self._keyword_opportunities(card)

        notes: list[str] = []
        src = card.get("welcome_source", "")
        if src == "SCRAPED_FALLBACK":
            notes.append("School missing from the client CMS file - Welcome text "
                         "taken from the live page instead")
        if any("WELCOME_CMS_PAGE_MISMATCH" in f for f in flags):
            notes.append("Live page Welcome DIFFERS from the client CMS text - "
                         "the published page may be stale")
        if any("REST_MAY_INCLUDE_WELCOME" in f for f in flags):
            notes.append("Welcome container not found on the live page; "
                         "page-scope results may overlap the Welcome text")
        if not card.get("welcome_found"):
            notes.append("No Welcome section found on the page")
        if any("SEMANTIC_FAILED" in f for f in flags):
            notes.append("AI evaluation incomplete - school needs a re-run")
        for f in flags:
            if "PENDING_G1_APPROVAL" in f:
                loc = self.register.get(sid, {}).get("locality", "?")
                notes.append(f"Verified locality '{loc}' awaits analyst approval")
                break
        for f in flags:
            if "FAR_LOCALITY_MENTION" in f:
                detail = f.split("FAR_LOCALITY_MENTION:", 1)[-1]
                name, _, dist = detail.partition("@")
                notes.append(f"Page mentions far-away area '{name}'"
                             + (f" ({dist.replace('mi', ' mi')} away - earns no credit)" if dist else ""))
        if any("BRAND_ONLY" in f for f in flags):
            notes.append("School name appears as brand only (no campus identifier)")
        if rejected:
            notes.append(f"{rejected} AI claim(s) rejected because the quoted "
                         f"text was not found on the page (see sheet 02)")
        return rejected, kw_opps, "; ".join(notes) if notes else "No issues flagged"

    # ---------------------------------------------------------------- pivots
    def _presence_pivot(self, wb, title: str, cards: list[dict],
                        params_config: list[dict], status_key: str,
                        evidence_key: str):
        """Wide matrix: rows = schools (ID/Name/URL), cols = all 56 parameters.

        Cell = status for the sheet's scope; PRESENT/PARTIAL cells embed the
        verbatim page quote so the justification is visible in place:
            PRESENT — "quoted text from the page"
        The 08 sheet uses rest_status (page WITHOUT the welcome section), so a
        parameter that lives only in the Welcome correctly shows ABSENT there.
        Evidence for the rest scope comes from page_evidence, which holds the
        rest-side quote whenever rest is PRESENT/PARTIAL.
        """
        pids = [p["id"] for p in params_config]
        names = {p["id"]: p.get("name", p["id"]) for p in params_config}
        params_by_id = {p["id"]: p for p in params_config}
        ws = self._sheet(wb, title, ["School ID", "School Name", "School URL"]
                         + [f"{pid}\n{names[pid]}" for pid in pids])
        for c in ws[1]:
            c.alignment = Alignment(horizontal="center", vertical="center",
                                    wrap_text=True)
        ws.row_dimensions[1].height = 42
        # hover tooltip on every parameter header: full name + what it checks
        for i, pid in enumerate(pids, start=4):
            self._param_tooltip(ws.cell(row=1, column=i), pid, params_by_id)

        for s in cards:
            row = [s.get("school_id"), s.get("school_name"), self._url(s)]
            fills = [None, None, None]
            for pid in pids:
                p = s.get("parameters", {}).get(pid, {})
                status = p.get(status_key, "ABSENT")
                if status in ("PRESENT", "PARTIAL"):
                    ev = p.get(evidence_key) or ""
                    if len(ev) > PIVOT_EVIDENCE_CHARS:
                        ev = ev[:PIVOT_EVIDENCE_CHARS] + "..."
                    cell_text = f'{status} — "{ev}"' if ev else status
                else:
                    cell_text = status
                row.append(cell_text)
                fills.append(_STATUS_FILLS.get(status))
            ws.append([_clean(x) for x in row])
            r = ws.max_row
            for idx, fill in enumerate(fills, start=1):
                if fill is not None:
                    ws.cell(row=r, column=idx).fill = fill

        ws.freeze_panes = "D2"
        ws.auto_filter.ref = ws.dimensions
        ws.column_dimensions["A"].width = 10
        ws.column_dimensions["B"].width = 34
        ws.column_dimensions["C"].width = 46
        from openpyxl.utils import get_column_letter as _gcl
        for i in range(4, 4 + len(pids)):
            ws.column_dimensions[_gcl(i)].width = 28
