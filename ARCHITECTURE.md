# lcg-utility — Fool-Proof Architecture & Build Plan

**Companion to:** [README.md](README.md) (project understanding). This document is the implementation blueprint.
**Prime directive:** this audit's output tells copywriters which location words to put on ~1,100 live school pages. A wrong neighborhood, street, or landmark recommendation is *worse than no recommendation* — it creates location misinformation, NAP inconsistency, and real local-SEO damage for the client. Therefore: **no location string may reach a score or a recommendation without a verified provenance chain, and anything below client-attested trust passes a human gate before it can be prescribed.**

**Evidence basis:** every number in this document was measured directly from the source files on 2026-10-05 (scripts in session scratchpad; re-runnable).

---

## 1. Measured Facts About the loc-intel Data (the reason for the gates)

| # | Finding | Measured value | Consequence |
|---|---|---|---|
| F1 | Output is **variable run-to-run** | 2026-09-19 run: 1,112 schools, 9,893 pairs (~8.9/school). 2026-09-21 re-run of 939 of the same schools: 3,364 pairs (~3.6/school) | The raw pair set is NOT a stable ground truth; only a filtered, frozen snapshot may be consumed |
| F2 | The variability is confined to the NEARBY discovery set | **LOCATED_IN agreement across the two runs: 925/925 = 100%** | The LOCATED_IN signal is reproducible and usable; NEARBY is not |
| F3 | "Confidence Level" carries **no independent information** | HIGH ⇔ LOCATED_IN (1,142 rows), MEDIUM ⇔ NEARBY (3,947 rows), perfectly correlated | We must never justify a decision with "loc-intel confidence was HIGH"; it is a relabeling of the relationship field |
| F4 | Every NEARBY candidate is **> 5 miles** from the school | 3,947/3,947 rows in the >5 mi bucket | NEARBY names are not the school's locality. They earn **zero credit** in scoring and are **never prescribed** (the earlier "NEARBY = PARTIAL" idea is dead) |
| F5 | Most LOCATED_IN "neighborhoods" are just the **city name fallback** | vs. schools' RIO cities: **354 true sub-city localities · 730 city-duplicates · 29 RIO schools with no LOCATED_IN · 8 loc-intel school IDs not found in RIO** | Only 354 schools have a usable verified locality. For 730 schools GEO_04 is **N/A** (city coverage is already GEO_02 — no double counting, no fabricated neighborhoods) |
| F6 | Even the 354 "true" rows contain anomalies | e.g. RIO city "Lacey North"/"Lacey South" (not real municipalities) with locality "Lacey"; "Zionsville" vs RIO city "Indianapolis"; subdivision-grade names ("Wheatstone Village Copperstone", "Caton Crossing") | A **one-time analyst review of all 354 rows** is mandatory before any of them can appear in a recommendation |
| F7 | Border/special cases are flagged upstream | 12 MULTI_COUNTY_BORDER, 101 INDEPENDENT_CITY, 16 FEDERAL_DISTRICT | Carry flags through to the review sheet; never strip them |

---

## 2. Location Truth Model (trust tiers)

Every location value in the system belongs to exactly one tier. The tier determines what the value is allowed to do. Every scored status and every remediation row carries `source_tier` + a human-readable provenance string.

| Tier | Source | Values | Allowed for **detection** (scoring) | Allowed for **prescription** (remediation text) |
|---|---|---|---|---|
| **0 — Client-attested** | `RIO_Location_Export.xlsx` (client's own location database) | School #, Location Name, Address 1, City, Region, Post Code, Website | ✅ yes | ✅ yes (it's the client's own data) — after ingest validation |
| **1 — Client-derived** | RIO | Lat/Lng, Google Place ID | provenance/verification only | ❌ never appears in copy recommendations |
| **2 — Discovered, filtered, human-approved** | loc-intel LOCATED_IN ∩ (locality ≠ RIO city), i.e. the **Verified Locality Register (354 rows)** | one locality name per school | ✅ yes (immediately, as *detection* target) | ⚠️ **only rows individually approved by an analyst** (one-time review pass) |
| **2x — Discovered, rejected** | loc-intel NEARBY set; city-duplicate LOCATED_IN rows | — | ❌ zero scoring credit; mentions logged as diagnostics only | ❌ never |
| **3 — Page-claimed** | the school page itself | landmarks, cross-streets, any locality phrasing found on page | ✅ for *this school's own page* (that's what the audit measures), with verbatim-evidence guard | ❌ never propagated as fact; landmark gaps become **field tasks** ("director to supply 1–2 nearby landmarks"), not auto-suggestions |

**Defense in depth — the same rule enforced three times:**
1. *Data layer:* the prescription builder can only read from `verified_locality_register.csv` rows where `approved == true`.
2. *Code layer:* an assertion raises if any remediation row contains a locality string absent from the approved register (or a landmark string at all).
3. *QA layer:* `output_validator.py` re-scans the finished workbook for the same invariants before it is delivered.

---

## 3. GEO_04 (Locality) Decision Matrix — exact rules

### Detection (what the scorer does)

| School cohort | Size | Rule |
|---|---|---|
| In Verified Locality Register | 354 | Normalized word-boundary match of the registered locality in **body copy** → Welcome/Page status PRESENT; not found → ABSENT. No PARTIAL from locality variants unless the analyst added an alias column entry |
| City-duplicate (locality ≡ RIO city) | 730 | **GEO_04 = N/A** — excluded from numerator *and* denominator. Rationale: no verified sub-city locality exists; the city itself is already scored as GEO_02. Anything else double-counts or fabricates |
| No LOCATED_IN / unresolved ID joins | 29 + 8 | **N/A + review list** (reconcile IDs; rural schools stay N/A) |
| Any school whose page mentions a locality-like name that is NOT its registered locality | any | Status unchanged; row added to `06_Location_Review` as **LOCALITY_CONFLICT** — the page may already carry wrong location info, which is itself a valuable client finding. We flag; we never auto-judge |
| Any school whose page mentions a name from the NEARBY set | any | No credit; diagnostic note only (`FAR_LOCALITY_MENTION`, with the measured distance) |

### Prescription (what the remediation queue may say)

| Case | Remediation text |
|---|---|
| Register school, row **approved**, GEO_04 ABSENT | `ADD locality "<name>"` + provenance: *"Google reverse-geocode at school coordinates (stable across runs); approved by <analyst> on <date>"* |
| Register school, row **not yet approved** | `LOCALITY CANDIDATE "<name>" — pending analyst approval` (queued on review sheet, NOT in copywriter queue) |
| City-duplicate school | *"No verified sub-city locality exists for this location. Strengthen city, street, and landmark signals instead."* — the system **never invents a neighborhood** |
| No-data school | *"Locality unknown — analyst to determine local community term before any copy change."* |

### Landmarks (GEO_06/07) — detect-only
- The LLM judges only what is **on the page**, must quote evidence verbatim, and Python verifies the quote exists in the scope text (hallucination guard). The LLM prompt includes Tier-0 identity (name, street, city) so it can judge "anchored to this school" — it is **never shown Tier-2 candidates or any landmark database**, so it cannot echo them back as findings.
- Missing-landmark remediation is always: **FIELD TASK — collect 1–2 genuine nearby landmarks from the school director.** Optional future enhancement (off by default, separately costed): Places-API cross-check of page-claimed landmarks against school coordinates, output as flags only.

---

## 4. System Architecture

### 4.1 Module map (→ = produces)

```
src/
├── ingest/
│   ├── rio_loader.py            RIO.xlsx → data/master_schools.csv + data/ingest_report.json
│   │                            validations: 1,113 rows; ZIP→5-char str; brand aliases
│   │                            (Childrens↔Children's, ATGW↔Gilden Woods, U Gro↔U-GRO);
│   │                            flags: 2 duplicate-URL pairs, brand=None, missing lat/lng,
│   │                            suspicious city values (e.g. "Lacey North")
│   └── locality_register.py     51 batch workbooks → (latest-wins, LOCATED_IN only, join on RIO)
│                                → data/verified_locality_register.csv   (354 rows, approved=blank)
│                                → data/locality_city_duplicates.csv     (730 — N/A cohort)
│                                → data/locality_unresolved.csv          (29 + 8 — review)
│                                → data/nearby_diagnostic_set.json       (detection diagnostics only)
│                                invariants: register re-derivation is byte-identical (snapshot_hash);
│                                every row carries source workbook + run date
├── matching/
│   ├── normalize.py             pure functions: NFKC + quote folding, lowercase, USPS suffix &
│   │                            directional expansion (Blvd↔Boulevard, N↔North), Saint↔St,
│   │                            word-boundary pattern builders. 100% unit-tested incl. known traps:
│   │                            "FL"⊄"flexible", "Beach Blvd"="Beach Boulevard", "Denny's"="Denny’s"
│   └── geo_matcher.py           GEO_01–GEO_05 + GEO_04 matrix (§3) + locality-conflict scan.
│                                returns {status, evidence, source_tier, provenance, flags}
├── scraper/
│   ├── page_fetcher.py  v2      ONE shared Chromium; context per page; per-domain throttle
│   │                            (≤1 req/s/domain + jitter) on top of global semaphore;
│   │                            validation gates before caching: HTTP 200, final URL still contains
│   │                            school slug/number, title or H1 matches school identity, min length;
│   │                            failures → data/quarantine/ + run manifest entry. NO requests fallback
│   │                            (proven blocked: curl hung & died vs. site bot mitigation)
│   └── section_extractor.py v2  brand selector profiles (config/brand_selectors.json; La Petite:
│                                div.welcomeMessage .welcome-content — verified live) → heading walk
│                                fallback with container size caps → welcome_found=false (NO fake
│                                "first 250 words" fallback). Three channels: welcome_text, body_text
│                                (nav/header/footer/cookie/nearby-schools stripped), boilerplate_text.
│                                Repeated-block dedup (verified: carousel triplicates content)
├── evaluator/
│   ├── deterministic_checker.py v2   word-boundary variant regex for KW/SKW + density counts;
│   │                                 STR_01 only when welcome_found; STR_02 on normalized text
│   ├── llm_client.py                 thin API wrapper: structured output (JSON schema), temp 0,
│   │                                 retries, prompt caching, cost metering, model pinning
│   └── semantic_evaluator.py v2      ONE call per school for all ~26 semantic params × 2 scopes,
│                                     governed by SKILL.md v2; evidence-verbatim guard; downgrade
│                                     + flag on unverified evidence; semantic_failed flag on
│                                     schema failure after 2 retries (school continues)
├── scoring/scoring_engine.py v2      N/A state (excluded from both numerator & denominator);
│                                     weight rule per decision D1; source_tier passthrough;
│                                     config_hash + model_id stamped into every observation
├── reporter/report_generator.py v2   sheets 00–06 (see §4.3); sanitization: illegal XML chars,
│                                     32k-char truncation, formula-injection prefix
└── qa/output_validator.py            post-run invariant checks (see §2 defense-in-depth layer 3)
```

### 4.2 Pipeline flow (per school, resumable at every arrow)

```
Phase 0 (once): rio_loader + locality_register  →  frozen inputs + review CSVs
                                                      │
                                   analyst approves 354-row register (one-time HITL)
                                                      │
run: FETCH ──► validate ──► cache ──► EXTRACT (3 channels, welcome_found)
      ──► DETERMINISTIC (geo_matcher + keyword engine)
      ──► SEMANTIC (1 LLM call, evidence guard)
      ──► SCORE (pure math, N/A-aware) ──► observations/{id}_eval.json
                                                      │
            REPORT (workbook + QA validator) ◄── all observations + run manifest
```

CLI: `--fetch-only | --eval-only | --rescore | --report-only | --force | --school <id>` ; cache invalidation by `config_hash`.

### 4.3 Deliverable workbook (unchanged sheets + two new)

| Sheet | Content |
|---|---|
| `00_Run_Summary` | snapshot window, config hash, model id, fetch failures/redirects/quarantines, N/A cohort sizes, cost |
| `01_School_Scorecard` | per-school scores, ranks, bands |
| `02_Parameter_Presence_Matrix` | 1,113 × 56 = 62,328 rows + `Source_Tier` + `Provenance` columns |
| `03_Parameter_Summary` | network benchmarks, MOVE vs CREATE counts |
| `04_Remediation_Queue` | copywriter tasks — **only Tier-0 and approved Tier-2 strings can appear; landmark gaps appear as field tasks** |
| `05_Evaluation_Evidence` | per-parameter evidence log, DOM paths, verified-evidence flags |
| `06_Location_Review` | LOCALITY_CONFLICTs, FAR_LOCALITY_MENTIONs, unresolved IDs, unapproved register rows, border/independent-city flags |

### 4.4 Data contracts (abbreviated)

`verified_locality_register.csv`:
`school_id, locality, locality_aliases, rio_city, state, relationship(LOCATED_IN), source_run_date, source_workbook, seo_flags, snapshot_hash, approved(bool), approved_by, approved_date, analyst_notes`

`observations/{id}_eval.json` (per parameter):
`{status: PRESENT|PARTIAL|ABSENT|N_A, welcome_status, page_status, evidence_welcome, evidence_page, evidence_verified(bool), source_tier, provenance, flags[], confidence}` + envelope `{config_hash, model_id, skill_version, fetch_ts, extractor_version, extraction_method, welcome_found}`

`run_manifest.json`: per-school `{state: FETCHED|EXTRACTED|EVALUATED|SCORED|FAILED|QUARANTINED, error_class, attempts, final_url, fetch_ts}`

---

## 5. HITL (human-in-the-loop) Gates

| Gate | When | Who | Blocking? |
|---|---|---|---|
| G1 — Locality register approval (354 rows, one spreadsheet pass) | after Phase 0 | SEO analyst | blocks *prescription* of localities only; detection & everything else proceeds |
| G2 — Golden calibration sign-off (10 schools vs. analyst grading, ≥95% agreement; PARTIAL definitions frozen) | end of build | analyst + you | blocks full-network run |
| G3 — Pre-delivery QA (output_validator + 30-school random manual sample + 06_Location_Review triage) | after full run | analyst | blocks delivery to client |

---

## 6. Build Order (each step has a hard acceptance gate; no step starts implementing the next before its gate passes)

| Step | Build | Done when (acceptance gate) | Needs decision? |
|---|---|---|---|
| **S1** ✅ **DONE 2026-10-05** | `rio_loader` + `locality_register` + ingest/consolidation reports | **Gate passed:** master CSV 1,113 rows, 27 anomaly rows flagged (incl. client data errors: school 646 URL points at 9108's page; RIO city typo "Chatlotte"); register = exactly 354 / 730 / 29+8, snapshot hash `4b783a64…c28f50` byte-identical across re-runs; sources frozen under `data/source/` (RIO sha256 `4f63ea16…3a29`, 51 workbooks) | none |
| **S2** ✅ **DONE 2026-10-05** | `normalize.py` + `geo_matcher.py`, TDD | **Gate passed:** 48/48 tests green — FL/flexible + IN/OR/ME traps, Blvd↔Boulevard (USPS suffix classes), curly quotes, Winston-Salem, St/Saint, Port-Charlotte≠Charlotte compound-prefix guard, guarded state abbreviations (comma/ZIP/city context, uppercase-only), city-in-nav-not-counted channel fixture, full GEO_04 register/N-A/conflict matrix; resolver smoke-tested against real S1 artifacts (354/730/37 cohorts load, pending-approval flag fires) | none |
| **S3** ✅ **DONE 2026-10-05** | `page_fetcher` v2 | **Gate passed:** 11/11 brand domains fetched & validated (one shared Chromium, per-domain ≤1 req/s + jitter, validation gates incl. redirect/identity checks, quarantine dir, run manifest); re-run = 11/11 CACHE_HIT; `city_in_identity` diagnostic recorded for dup-URL rows like school 646; no plain-HTTP fallback | none |
| **S4** ✅ **DONE 2026-10-05** | `section_extractor` v2 + `brand_selectors.json` | **Gate passed:** all 11 brands extract via BRAND_SELECTOR — **every brand shares the LCG template** `div.welcomeMessage > .welcome-content` (verified incl. Montessori & Everbrook), so one default profile + heading-walk fallback covers the network; welcome_found=True ×11, word counts 114–242, zero nav leaks into body channel, 7440 known-good (214w, landmark sentence intact); carousel dedup + body_rest channel working; 55/55 tests green | none |
| **S5** ✅ **DONE 2026-10-05** | `deterministic_checker` v2 wired to matcher | **Gate passed:** 71/71 tests green; 7440 hand-audit 11/11 exact (GEO_01/02/03/05, KW_01–04, TECH_01 without ®, STR_01/02); three-scope model (welcome / rest / combined page) with STR rules pinned to page=N_A; KW/SKW variants moved into `parameters_database.json` (incl. prek/pre-kindergarten; bare "drop-in" removed — false-positives on "drop in for a tour"); density counts per scope; 11-school smoke sane. Live finding: 7440 is a register school ("Golden Glades/The Woods") — slash-compound locality, prime G1 review example | none |
| **S6** 🟡 **BUILT 2026-10-05 — live smoke pending API key** | `llm_client` + `semantic_evaluator` v2 + SKILL.md v2 | Code + unit gate passed (mocked client): evidence-verbatim guard (incl. curly-quote folding + wrong-scope rejection), landmark generic-geo guard (token-based — substring version had an "fl"-inside-"florida" bug caught by tests), upgrade-only keyword paraphrase merge, failure→N_A containment, Tier-2 payload isolation verified; end-to-end dry run on real 7440 artifacts caught a seeded hallucination. **Remaining:** one live call on 7440 (GEO_06 "across from Denny's") once `ANTHROPIC_API_KEY` is provided. Default model `claude-opus-5-5` (env `LCG_LLM_MODEL` to override); no temperature param (rejected by model); rubric prompt-cached | **API key** (blocks live smoke only) |
| **S7** ✅ **DONE 2026-10-05** | `scoring_engine` v2 | **Gate passed:** hand-calculated fixtures exact incl. N_A exclusion from both numerator & denominator (separate welcome/page denominators; STR weights leave page denominator), location-state matrix from welcome×rest (WELCOME_ONLY now reachable), config-hash cache invalidation, envelope recording, confidence mean excludes unverified evidence; 92/92 tests green | D1 ✅ applied |
| **S8** ✅ **DONE 2026-10-05** | `report_generator` v2 + `output_validator` + `main.py` rewire | **Gate passed:** 107/107 tests incl. seeded-violation suite (unapproved locality, NEARBY name, suggested landmark, formula injection, evidence-less PRESENT all caught); real end-to-end run on the 11-school sample via `main.py --skip-semantic` → 7-sheet workbook (matrix 616 rows, queue 213, review 36) → validator **PASS V1–V7**. The validator proved itself live: first run caught 8 real evidence-fallback gaps in the checker, fixed; `PIPELINE_VERSION` now feeds the config hash so code-behavior changes auto-invalidate cached observations. CLI: `--fetch-only / --report-only / --skip-semantic / --force / --schools` | none |
| **S9** | Golden calibration — 10 schools (3 strong/3 avg/3 sparse/1 edge; incl. 1 Montessori, 1 Gilden Woods) + G1 register approval + G2 sign-off | ≥95% analyst agreement; PARTIAL rules frozen; register approved | analyst time |
| **S10** | Full 1,113 run + G3 QA + delivery | manifest shows ≥98% SCORED; 06_Location_Review triaged; validator green | **D2 (Childcare Network?)** before scoping final list |

Parallelizable: S3/S4 can proceed while S2 tests are being written; S6 is independent of S3–S5 once extraction fixtures exist.

---

## 7. Misinformation Risk Register (threat → safeguard)

| Threat | Safeguard(s) |
|---|---|
| Wrong neighborhood recommended to copywriters | Tier model §2; 730 city-duplicates forced to N/A; G1 approval of all 354 names; triple enforcement (data/code/QA) |
| NEARBY (>5 mi) name treated as locality | F4 rule: zero credit, never prescribed, diagnostic-only |
| loc-intel run variability leaking into results | only the run-stable LOCATED_IN signal consumed (F2); frozen snapshot with hash; NEARBY set quarantined to diagnostics |
| False "verified" aura from loc-intel confidence | F3: confidence field ignored for decisions; our provenance strings name the actual method ("Google reverse-geocode at school coordinates") |
| LLM hallucinating landmarks/localities | evidence-verbatim guard; LLM never shown Tier-2/Tier-3 candidate lists; landmark remediation = field task only |
| Nav/footer/sibling-school text scored as local presence | 3-channel extraction; S4 gate explicitly tests nav city-lists don't leak |
| State false positives ("FL" in "flexible") | word-boundary matching, S2 trap tests |
| Street-name formatting mismatch producing false ABSENT → needless "CREATE" tasks | USPS suffix/directional normalization (S2) |
| Page already contains wrong location info | LOCALITY_CONFLICT flag → 06_Location_Review (reported, never auto-"corrected") |
| Stale rules silently reusing cached verdicts | config_hash + model pinning in every observation |
| Existing-page errors amplified: two schools share one URL | fetched once, both flagged `DATA_DUP_URL`, excluded from remediation until client resolves |

---

## 8. Open Decisions (blocking markers in §6)

- **D1 — DECIDED 2026-10-05:** must-have ⇒ weight 4.0 regardless of priority. Applied to `config/parameters_database.json` (7 parameters reweighted: GEO_01–GEO_06, STR_02; KW_01–04 already 4.0; 11 must-have total).
- **D2 — DEFERRED by user 2026-10-05:** Childcare Network decision to come later; scope proceeds as recommended with the 1,113 RIO schools.
- **D3** (default set, confirm at G2): visible-body-copy-only scoring, JSON-LD/footer as diagnostic columns.
- **D4**: duplicate-URL pairs — two scorecard rows or one merged row? (default: two rows, flagged)
- **D5** (frozen at G2): PARTIAL definitions per parameter class.
- **D6**: who performs G1 register approval, and target date — it's one pass over one 354-row sheet.

---

*Prepared 2026-10-05. All measurements reproducible; analysis scripts available in the session scratchpad and can be committed under `tools/analysis/` on request.*
