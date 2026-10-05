# LCG Local SEO: School Welcome Section & Whole-Page Audit Utility (`lcg-utility`)

**Scope:** 1,113 school pages · 11 LCG brands · 56 atomic parameters · 2 content boundaries per school
**Deliverable:** `LCG_Welcome_SEO_Audit.xlsx` (multi-sheet executive scorecard + remediation queue)
**Status:** Scaffolded, not yet production-ready — see [Part 5: Implementation Status](#part-5--current-implementation-status-honest-audit) for the verified gap list.
**Last investigated:** 2026-10-05 (all facts below were verified against live data/code on this date, not assumed).

---

## What This Is

A reproducible Python audit pipeline that scrapes every LCG school page, splits it into two content boundaries, and evaluates 56 canonical local-SEO parameters in each:

1. **Welcome Section Score (S_welcome)** — quality/completeness of the high-converting *"Welcome to Our School"* block.
2. **Whole Page Score (S_page)** — presence of the same parameters anywhere on the page.
3. **Placement Gap (S_gap = S_page − S_welcome)** — signals that exist on the page but are missing from the Welcome section (**MOVE/SURFACE**) vs. signals missing everywhere (**CREATE**).

Deterministic checks (regex/ground-truth matching) run in Python. Subjective checks (landmarks, accreditation, culture) run through an LLM governed by `skills/welcome_seo_evaluator/SKILL.md`. **Scores are always computed by Python math, never by asking the LLM for a number.**

---

## Part 1 — Core Execution Questions, Answered

### Q1. How does the system distinguish between the addresses/cities/localities/landmarks mentioned on a page?

A school page mentions many locations that are **not** the school's own: nav menus list sibling cities, "Other schools near you" widgets list sibling schools' street addresses, the footer carries corporate addresses, and FAQ/JSON-LD blocks repeat them all. Naive text search would mark GEO parameters PRESENT from the wrong mention. The system disambiguates in four layers:

1. **Ground-truth anchoring.** Every school's canonical identity comes from `RIO_Location_Export.xlsx` (School #, Location Name, Address 1, City, Region, Post Code, Lat/Lng, Website, brand). A GEO parameter only counts if the matched string equals (post-normalization) **this school's own** value — a different school's address on the same page is ignored.
2. **DOM scoping.** Whole-page text is extracted from `<main>`/content containers with global `<header>`, `<nav>`, `<footer>`, cookie banners, and "nearby schools" widgets stripped into a separate channel. A city name appearing only in the nav does not count as page presence (verified risk: nav menus on brand sites list dozens of cities).
3. **Normalization before matching.** Street-suffix expansion (Blvd↔Boulevard, Dr↔Drive, Tpke↔Turnpike, Expwy↔Expressway), directionals (N↔North), apostrophe variants (`'` vs `’` — the live sample page writes "Denny’s" with a curly quote), case, `St.`↔`Saint`, hyphenation. **Verified concrete case:** school 7440's `<title>` says "Beach Blvd." while its body copy says "Beach Boulevard" — exact-string matching fails without this layer. Match the *street-name token* ("Beach", "Mopac") + suffix class, not the full raw string.
4. **Hierarchy of sources per parameter class:**
   - `GEO_02 City`, `GEO_03 State`, `GEO_05 Street` → deterministic vs. RIO ground truth (word-boundary regex, never substring — see FL bug in Part 5).
   - `GEO_04 Locality` → deterministic lookup against the **Verified Locality Register** distilled from `rankuno-loc-intel` (LOCATED_IN signal only — measured 100% stable across the tool's two runs; USPS postal-hub corrections like Wheaton↔Silver Spring preserved). Only **354 schools** have a true sub-city locality; for **730 schools** the tool's "neighborhood" is just the city name (its documented fallback), so GEO_04 is **N/A** there — never double-counted against GEO_02. NEARBY candidates (all measured >5 mi away) earn **zero credit and are never recommended**. Full decision matrix + trust tiers: [ARCHITECTURE.md §2–3](ARCHITECTURE.md).
   - `GEO_06/07 Landmarks` → LLM semantic check for *spatial-relationship phrasing anchored to the school* ("across from Denny's", "between Hodges Boulevard and San Pablo Road"), with a hard rule: the quoted evidence snippet must literally exist in the page text, and city/state names alone never qualify as landmarks.

### Q2. Does Python process all 1,113 URLs at once?

**No.** An `asyncio` worker pool with a bounded semaphore (default 5 concurrent) walks the list. Full parallelism would (a) hammer a handful of brand domains — 461 of the pages live on `lapetite.com` alone — triggering bot mitigation, (b) spike memory with 1,113 live browser pages, and (c) lose everything on a crash. Required refinements (not yet implemented): **per-domain throttling** (the global semaphore still lets all 5 workers hit the same domain), jitter between requests, and one shared Chromium instance instead of a fresh browser launch per page.

### Q3. Is the content stored somewhere?

**Yes — three persistent layers under `data/`, each a resume checkpoint:**

| Layer | Path | Purpose |
|---|---|---|
| Raw HTML | `data/raw_html/{school_id}.html` | Rendered-DOM snapshot; re-evaluation never re-fetches |
| Extracted sections | `data/extracted_sections/{school_id}.json` | Welcome text, whole-page text, DOM path, word counts |
| Observations | `data/observations/{school_id}_eval.json` | Per-parameter statuses, evidence snippets, confidences, scores |

This makes the pipeline **re-entrant at every stage**: change the SKILL rubric → re-run semantic eval from cached extractions (no scraping cost); change weights → re-score from cached observations (no LLM cost). Caveat: observations must embed a **config hash + model version** so stale caches invalidate when rules change (currently missing — see Part 5).

### Q4. Per-school full cycle, or stage-by-stage for all schools?

**Per-school complete lifecycle, batched:** each worker takes one school through `FETCH → EXTRACT → DETERMINISTIC → SEMANTIC → SCORE → PERSIST` and writes the observation before moving on. School #451 starts whether or not school #27 failed. Interruption at school 450 resumes at 451. Aggregation (Excel workbook) runs once at the end over all persisted observations.
The stage-wise alternative (fetch all → extract all → …) is supported *implicitly* through the caches: proposed CLI flags `--fetch-only`, `--eval-only`, `--rescore`, `--report-only` re-run any single stage from cache. This hybrid is deliberate: per-school for fault isolation and resumability, stage flags for cheap iteration during calibration.

### Q5. Should the head keywords (Daycare, Preschool, Childcare, Early Education) also have semantic detection?

**Yes — they are Hybrid, two-pass:**
1. **Deterministic pass (fast, free):** word-boundary regex across variants (`daycare|day care|day-care`, `pre-k(?!indergarten)?`…) → exact presence + occurrence counts per scope (density profile).
2. **Semantic pass (LLM):** catches what regex can't:
   - **Paraphrase coverage** — "infant care program", "early learning center", "pre-kindergarten program" satisfy the *topical intent* without the literal string → PARTIAL instead of false ABSENT.
   - **Context classification** — distinguishes an actual service claim ("quality daycare in Jacksonville") from negation ("more than just a daycare"), from nav/footer boilerplate, from testimonial quotes.
   - Status resolution: exact match in body copy → PRESENT · semantic-only paraphrase or nav-only exact match → PARTIAL · neither → ABSENT.

---

## Part 2 — Verified Data Landscape (what actually exists upstream)

### 2.1 `rankuno-loc-intel\RIO_Location_Export.xlsx` — master ground truth ✅ verified

- **1,113 rows**, School # unique, sheet `Locations-Export`.
- Columns: School #, Location Name, Address 1/2, Suite, City, Region, Post Code, Lat, Lng, Website, brand, Google Place ID, Google Maps URL, Optimal Radius.
- Brand counts (exact strings as stored): La Petite Academy 461 · Childtime 249 · Tutor Time 122 · The Childrens Courtyard 98 · Everbrook Academy 85 · Montessori Unlimited 28 · ATGW 23 · Pathways Learning Academy 16 · U Gro 14 · Creative Kids 10 · Young School 6 · **`None` 1**.
- **Data quirks found (must handle in Phase 0):**
  - ⚠️ **2 duplicate Website URLs** (two Childtime schools share `chicago-il-9108`, two Tutor Time share `middle-island-ny-6684`) → fetch once, attribute to both, flag as data errors for the client.
  - ⚠️ **1 school missing Lat/Lng**, **1 with brand `None`**, **37 with Optimal Radius "not available"**.
  - ⚠️ **Brand-string drift:** RIO stores "The Childrens Courtyard" (no apostrophe); the live site says "The Children's Courtyard". ATGW's domain is `gildenwoods.com` (pages brand as "Gilden Woods"). "U Gro" is `u-gro.com` ("U-GRO" on site). GEO_01 matching must be apostrophe-/hyphen-insensitive and brand-alias-aware.
  - ⚠️ **"Childcare Network" does not exist in RIO** although the original project overview names it as an LCG brand. Open question for the client: in scope or not? (If yes, we're missing its URL list entirely.)
  - 41 states covered; no leading-zero ZIP corruption found (verified), but ZIPs must still be normalized to 5-char strings on ingest.
- Domain distribution confirms **12 distinct site templates** to support: lapetite.com, childtime.com, tutortime.com, childrenscourtyard.com, everbrookacademy.com, montessori.com, gildenwoods.com, pathwayslearningacademy.com, u-gro.com, creativekidslearningcenter.com, youngschool.com (+1 row with bare `childrenscourtyard.com`, no `www`).

### 2.2 Neighborhood intelligence (GEO_04 source) ✅ verified — ⚠️ usable only after filtering + human approval

> Measured in depth on 2026-10-05; this section supersedes any earlier framing of the loc-intel output as ready-made ground truth. Full trust model: [ARCHITECTURE.md](ARCHITECTURE.md).

- Raw coverage: **1,112 / 1,113 schools** across **51 separate batch workbooks** (SQLite cache tables are empty except 1,287 logged exceptions: `PRIMARY_ZIP_MISSING` 945 · `NO_NEIGHBOURHOODS_FOUND` 174 · `NO_ZIP_FOUND` 168).
- **The output is variable run-to-run**: the 2026-09-19 run yielded ~8.9 candidates/school; the 2026-09-21 re-run of 939 of the same schools yielded ~3.6/school. The instability is entirely in the NEARBY discovery set.
- **The LOCATED_IN signal is 100% reproducible** (925/925 schools identical across both runs) — it is the only part we consume.
- **"Confidence" has no independent meaning** (HIGH ⇔ LOCATED_IN, MEDIUM ⇔ NEARBY, perfectly correlated) and all NEARBY candidates sit **>5 miles** out — so NEARBY earns zero scoring credit and is never recommended.
- Joined against RIO cities: **354 true sub-city localities · 730 city-name fallbacks (GEO_04 = N/A) · 29 schools with none · 8 unresolved ID joins**. Even the 354 contain anomalies (RIO city "Lacey North", "Zionsville" vs "Indianapolis", subdivision-grade names) → **one-time analyst approval of all 354 rows (Gate G1) before any locality can appear in a remediation recommendation**.
- USPS postal-hub corrections are real and preserved (verified: "Wheaton: Silver Spring → Wheaton").
- **Phase 0 task:** `locality_register.py` distills the 51 workbooks into `data/verified_locality_register.csv` (354 rows, approval columns) + city-duplicate and unresolved lists, with snapshot hash and per-row provenance.

### 2.3 Live site reconnaissance ✅ verified 2026-10-05 against school 7440

- **Plain HTTP clients are blocked**: `curl` with a browser User-Agent hung past its 40s limit and died (exit 34) — TLS-fingerprint-level bot mitigation. **The `requests` fallback in the current fetcher is therefore useless against these sites** and must be removed or demoted to a flagged-failure path.
- **Playwright Chromium succeeds**: HTTP 200, no redirect, 179KB rendered HTML.
- The Welcome section exists exactly as the spec assumes, with a **stable CSS hook**: `div.welcomeMessage > .welcome-content > h2 ("Welcome to Our School") + div.welcome-columns > p*`. Per-brand selector profiles are viable and far more reliable than generic heading-walking.
- The real Welcome copy for 7440 contains, in ~230 words: city+state, street ("on Beach Boulevard"), cross-streets + landmark ("between Hodges Boulevard and San Pablo Road, across from Denny's"), longevity ("Since 1991"), NECPA + Gold Seal (QUAL_01), CPR/First Aid (QUAL_02), combined tenure ("100 years"), SproutAbout + live streaming (TECH_01/INC_02/INC_03), named elementary schools for before/after care, and a tour CTA. The parameter framework maps 1:1 onto real content. ✔
- **Trap confirmed:** page body writes "SproutAbout" **without ®** — the current checker searches the literal string `SproutAbout®` and would return a false ABSENT.
- **Trap confirmed:** "Meet Our Staff / Dee Pryor, Director" appears 3× in the heading list (carousel duplicating DOM nodes) → raw text extraction double/triple-counts content; deduplicate repeated blocks before word counts.

---

## Part 3 — Canonical Parameter Database

> **Count correction (important):** the original spec says "48 atomic parameters" and sizes the matrix sheet at "37,400+ rows". The actual atomic count is **56** (17 GEO + 5 KW + 17 SKW + 7 INC + 8 THM + 2 STR — the spec's own tables sum to 56), and the matrix is **1,113 × 56 = 62,328 rows**. `config/parameters_database.json` already (correctly) holds 56. All documents should standardize on 56.

### Category A — School & Geographic Variables (17)

| ID | Parameter | Detection | Priority | Must | Ground Truth | Evidence criteria |
|---|---|---|---|---|---|---|
| GEO_01 | School Name | Deterministic/Fuzzy | Medium | Yes | RIO `Location Name` | Branded name; apostrophe/alias-insensitive (Childrens↔Children's, ATGW↔Gilden Woods) |
| GEO_02 | City | Deterministic | High | Yes | RIO `City` | Word-boundary match, body scope only |
| GEO_03 | State | Deterministic | High | Yes | RIO `Region` | Full name OR abbreviation **with word boundaries** (never substring — "FL" ⊄ "flexible") |
| GEO_04 | Locality / Community | Hybrid | High | Yes | Verified Locality Register (354 schools) | Registered locality in body copy = PRESENT; city-fallback cohort (730) = N/A; NEARBY names = zero credit (see ARCHITECTURE.md §3) |
| GEO_05 | Street Address | Deterministic | High | Yes | RIO `Address 1` | Street-name token + suffix-class match (Blvd↔Boulevard) |
| GEO_06 | Landmark 1 | Semantic (LLM) | High | Yes | LLM rules | Spatial phrasing anchored to school; snippet must exist verbatim |
| GEO_07 | Landmark 2 | Semantic (LLM) | High | No | LLM rules | Secondary proof point |
| LEAD_01 | Director Name | Hybrid | Medium | No | Website | Named leadership (e.g. "Dee Pryor, Director") |
| LEAD_02 | Director Experience | Hybrid | Medium | No | Website | Quantified tenure |
| LEAD_03 | Years Serving Community | Hybrid | High | No | ⚠️ no CSV column exists — source is the page itself ("Since 1991") or client data | Longevity claim |
| QUAL_01 | Accreditation | Semantic | High | No | Website | NECPA, Gold Seal, NAEYC, Cognia… |
| QUAL_02 | Certifications | Semantic | High | No | Website | CPR, First Aid, state certified |
| QUAL_03 | Awards | Semantic | High | No | Website | Recognition/awards |
| QUAL_04 | Teacher Tenure | Semantic | High | No | Website | Staff stability metrics |
| FAC_01 | Facility Highlight | Semantic | High | No | Client | STEAM studio, shaded playground… |
| TECH_01 | Technology Feature | Deterministic | Medium | No | Brand rule | `SproutAbout` (match **without** requiring ®) all brands except Montessori Unlimited → `Montessori Compass` |
| SAFE_01 | Safety Feature | Semantic | High | No | Website | Keypad entry, secure check-in… |

### Category B — Primary Head Keywords (5) — all **Hybrid** (regex + LLM context pass, per Q5)

| ID | Keyword | Variants | Priority | Must |
|---|---|---|---|---|
| KW_01 | Daycare | daycare, day care, day-care | High | Yes |
| KW_02 | Preschool | preschool, pre-school, pre-k | High | Yes |
| KW_03 | Childcare | childcare, child care, child-care | High | Yes |
| KW_04 | Early Education | early education, early learning | High | Yes |
| KW_05 | Flexible/Drop-in Care | flexible care, part-time care, drop-in, flexcare | High | No |

### Category B.2 — Semantic Keywords (17, SKW_01–SKW_17, all High)

early education · child care · flexible daycare · kindergarten readiness · learning center · early childhood education · licensed child care · developmental milestones · individualized learning · tuition assistance · locally rooted school · age-appropriate learning · creative learning · early childhood development · foundational skills · near you · affordable

> ⚠️ `SKW_16 "near you"` and `SKW_17 "affordable"` are boilerplate magnets (cookie banners, CTA buttons, footer). Count only in body-copy scope.

### Category C — All-Inclusivity (7) + Content Themes (8)

| ID | Signal | Detection | Priority |
|---|---|---|---|
| INC_01 | Meals & infant formula provided | Semantic | High |
| INC_02 | Live video streaming | Semantic | High |
| INC_03 | Real-time app tracking | Semantic | High |
| INC_04 | Extended operating hours | Semantic | High |
| INC_05 | Age continuity (6 wks → school age) | Semantic | High |
| INC_06 | On-site extracurricular enrichment | Semantic | High |
| INC_07 | Elementary bus transportation | Semantic | High |
| THM_01 | Programs section cross-link | Det./LLM | Medium |
| THM_02 | Kindergarten transition | Semantic | Medium |
| THM_03 | Ages served callout | Det./LLM | High |
| THM_04 | School culture & family experience | Semantic | Medium |
| THM_05 | Teacher qualifications brief | Semantic | Medium |
| THM_06 | Tour invitation CTA | Det./LLM | Medium |
| THM_07 | School philosophy/approach | Semantic | High |
| THM_08 | Developmental benefit language | Semantic | Medium |

### Category D — Structural Rules (2)

| ID | Rule | Pass criteria | Priority |
|---|---|---|---|
| STR_01 | Word count | Welcome section 180–300 words (PARTIAL 120–400) — **only when a real Welcome section was found**, never on fallback text | Medium |
| STR_02 | Leads with local info | First 2 sentences of Welcome contain city/street/landmark, not generic brand copy | High |

### Four-state placement model (computed per parameter, per school)

```
🟢 BOTH             present in Welcome AND elsewhere   → OPTIMAL
🟡 WHOLE_PAGE_ONLY  missing in Welcome, on page        → MOVE / SURFACE
🔵 WELCOME_ONLY     in Welcome, missing elsewhere      → KEEP / REINFORCE
🔴 ABSENT           missing everywhere                 → CREATE NEW CONTENT
⚪ N/A              not applicable to this school      → excluded from denominators
```

---

## Part 4 — Architecture

### 4.1 Pipeline (per-school state machine, resumable at every arrow)

```
RIO ingest ──► master_schools.csv (1,113 rows, Phase 0)
loc-intel  ──► neighborhood_lookup.json (Phase 0)
                     │
   ┌─────────────────▼──────────────────────────────────────────────┐
   │ per school (async worker, semaphore + per-domain throttle):    │
   │  FETCH (Playwright, shared browser) ─► validate ─► cache HTML  │
   │  EXTRACT (brand selector profile → heading walk → flagged)     │
   │     ├─ welcome_text (+found flag, DOM path, word count)        │
   │     ├─ body_text   (main content, nav/footer stripped, dedup)  │
   │     └─ boilerplate channel (nav/footer/cookie text, separate)  │
   │  DETERMINISTIC (word-boundary regex vs ground truth)           │
   │  SEMANTIC (1 LLM call: all ~26 params × both scopes, JSON      │
   │            schema enforced, evidence-verbatim guard)           │
   │  SCORE (pure Python weights) ─► observations/{id}_eval.json    │
   └─────────────────────────────────────────────────────────────────┘
                     │
   REPORT (openpyxl) ─► LCG_Welcome_SEO_Audit.xlsx (6 sheets)
```

### 4.2 Section extraction fallback hierarchy

1. **Brand selector profile** (`config/brand_selectors.json`, built during golden calibration): e.g. La Petite → `div.welcomeMessage .welcome-content` (verified live).
2. **Heading walk**: h1–h4 matching `/welcome to our school/i` → stricter patterns first; container = nearest section-like ancestor, **capped** (reject if container text > ~60% of page text — guards against grabbing the whole page).
3. **No-welcome flag**: if neither hits, set `welcome_found=false`, Welcome scope scores all-ABSENT, school is routed to a manual-review list. **Never silently substitute "first 250 words"** — that fabricates a Welcome section out of nav text and poisons STR_01/STR_02 (current code does this; must change).

### 4.3 Semantic evaluator (the real one — current module is heuristic-only, see Part 5)

- **One call per school** (not per parameter): rubric (SKILL.md, prompt-cached) + welcome_text + body_text → JSON for all semantic parameters × both scopes. Cuts 1,113 × 26 calls down to 1,113 calls.
- Structured output / JSON-schema enforcement; temperature 0; model + prompt version recorded in each observation.
- **Hallucination guard:** every PRESENT/PARTIAL must carry an evidence snippet that is verified *verbatim* (post-whitespace-normalization) against the actual scope text in Python. Snippet not found → status downgraded to ABSENT + `evidence_unverified` flag. LLM self-reported confidence is recorded but never trusted as the sole gate.
- Retry-on-invalid-JSON (max 2), then flag school `semantic_failed` and continue.

### 4.4 Storage & reproducibility

- Every observation embeds: `config_hash` (parameters_database.json + SKILL.md digest), `model_id`, `fetch_timestamp`, `extractor_version`. Cache hit requires hash match; `--rescore` recomputes scores from cached statuses, `--force` refetches.
- Run manifest `data/run_manifest.json`: per-school state (FETCHED/EXTRACTED/EVALUATED/SCORED/FAILED + error class), enabling exact resume and a fetch-failure report sheet.

---

## Part 5 — Current Implementation Status (honest audit)

Verified by code reading + live tests on 2026-10-05. The scaffold is a good skeleton; **it has never completed a real run** (Playwright was not even installed until today; `data/raw_html` is empty).

### Working as intended ✔
- Project structure, config loading, CLI args, semaphore-bounded async orchestration (`main.py`)
- Three-layer disk caching design and resume-by-observation short-circuit
- Scoring math (weights × status values, four-state classification) matches spec
- Excel generator produces all 5 sheets with styling
- `config/parameters_database.json` — 56 parameters with weights/priorities ✔

### Critical defects found 🔴 (each independently invalidates results)

| # | Defect | Location | Consequence |
|---|---|---|---|
| 1 | **`SemanticEvaluator` never calls an LLM** — it's keyword lists with hardcoded confidences (0.90/0.85) presented as "AI confidence"; SKILL.md is loaded by nothing | `semantic_evaluator.py` | All 26 semantic params are cheap heuristics; e.g. GEO_06 "landmark" fires on the word "near" — "near you" marks every page PRESENT. Confidence sheet is fiction |
| 2 | **Substring matching without word boundaries** | `deterministic_checker.py:_evaluate_term` | `"fl" in "flexible"` → State=FL is PRESENT on essentially every page; same for IN/OR/ME/AL/DE… All GEO_03 results invalid |
| 3 | **`asyncio.gather()` without `return_exceptions`** | `main.py:93` | One school's exception kills the whole 1,113 run after hours of work; no failure manifest exists |
| 4 | **Fresh Chromium launch per page** + fixed 1.5s wait + no HTTP-status/redirect/content validation before caching | `page_fetcher.py` | ~1,113 browser launches; blocked/404/redirected-to-homepage HTML gets cached and scored forever (cache poisoning) |
| 5 | **`requests` fallback silently caches non-JS HTML** — and live test shows these sites tarpit non-browser clients anyway | `page_fetcher.py:66` | Hangs or poisons cache; must be removed/flagged |
| 6 | **Whole-page text includes nav/header/footer** (only script/style/svg stripped) | `section_extractor.py` | Nav city lists, sibling-school addresses, cookie banners all count as "page presence" → false MOVE recommendations at scale |
| 7 | **Welcome fallback = first 250 words of page** (which begins with nav text) with no `welcome_found` flag | `section_extractor.py:78` | Fabricated Welcome sections; STR_01 scores PRESENT on garbage |
| 8 | **`TECH_01` searches literal `SproutAbout®`** — live page says "SproutAbout" without ® | `brand_defaults.json` + checker | False ABSENT on the flagship tech parameter |
| 9 | **`brand_defaults.json` keys don't match RIO brand strings** (has "Childcare Network", "Discovery Point", "Pathway…"; missing Childtime, ATGW, The Childrens Courtyard, U Gro, Pathways…) | `brand_defaults.json` | Brand mapping silently defaults; Montessori-vs-Sprout logic only works by accident |
| 10 | **`data/master_schools.csv` is fabricated placeholder data** — school 7440 listed as "of West Ashley" in Jacksonville with street "7440 Beach Blvd" (id reused as house number); live page proves it's "La Petite Academy on Beach Blvd."; West Ashley is in Charleston SC; "Childcare Network" row's brand doesn't exist in RIO | `data/master_schools.csv` | Must be regenerated from RIO in Phase 0 |

### Material defects 🟡
- BS4 `find_all(text=...)` deprecated and only matches exact full text nodes; the lambda fallback then matches *any* heading containing "welcome" (e.g. hero "Welcome to La Petite!").
- Container text double-counting: `find_all(["p","li","span",…])` extracts nested spans twice; carousels (verified: 3× "Meet Our Staff") inflate counts further — dedupe repeated blocks.
- `is_observed()` short-circuit has no config-hash check → rule changes silently don't propagate.
- STR_02 `whole_page_evidence` claims "City found on page" even when it wasn't; STR_02 should also use normalized matching.
- No N/A status exists (see Part 7); `status_value_map.get(…, 0.0)` punishes inapplicable params.
- Excel: no `IllegalCharacterError` sanitization (scraped control chars crash openpyxl), no 32,767-char cell truncation, no formula-injection guard (snippets starting with `=`), 05_Evidence sheet lacks the per-parameter audit log the spec promises.
- No robots.txt check, no per-domain rate limit, no politeness jitter.
- Duplicate-URL pair handling absent (two schools double-fetch the same page concurrently).

---

## Part 6 — Edge Cases & Breaking Points Register

Organized by pipeline stage. **BP** = breaking point (stops/invalidates the run), **EC** = edge case (wrong result for some schools if unhandled). Items marked ✅ were directly verified during investigation.

### Stage 0 — Ingest (RIO + loc-intel)
| Type | Case | Handling |
|---|---|---|
| EC ✅ | 2 school pairs share one URL | Fetch once, attribute observation to both schools, flag `DATA_DUP_URL` for client |
| EC ✅ | 1 school brand=None, 1 missing lat/lng | Manual-assign brand from domain; lat/lng only needed for GEO_04 provenance |
| EC ✅ | Brand aliases (Childrens/Children's, ATGW/Gilden Woods, U Gro/U-GRO) | Alias table in brand config; normalize before GEO_01 matching |
| EC ✅ | Only 354 schools have a true sub-city locality; 730 are city-name fallbacks; 29+8 unresolved | GEO_04 = N/A outside the register; never fabricate a neighborhood; unresolved IDs reconciled in Phase 0 |
| BP ✅ | loc-intel output varies run-to-run; only LOCATED_IN is stable (100% agreement) | Consume LOCATED_IN only, frozen snapshot with hash; NEARBY set quarantined to diagnostics; analyst approval gate (G1) before prescription |
| BP ✅ | Neighborhood intel scattered across 51 workbooks; SQLite cache empty | Phase 0 `locality_register.py` with latest-wins merge + byte-stable re-derivation assertion |
| EC | ZIPs/Post Codes typed as ints | Cast to zero-padded 5-char strings on ingest |
| EC ✅ | "Childcare Network" in project brief but absent from RIO | Client decision required before Phase 4 |

### Stage 1 — Fetching
| Type | Case | Handling |
|---|---|---|
| BP ✅ | Bot mitigation tarpits non-browser clients (curl hung >40s, exit 34) | Playwright-only; remove `requests` fallback; realistic UA; consider headed mode if headless gets blocked mid-run |
| BP | Mid-run IP blocking after N requests (461 pages on one domain) | Per-domain throttle (≤1–2 req/s), jitter, exponential backoff on 403/429, checkpoint + resume-next-day capability (same pattern loc-intel used for API quota) |
| EC | Closed/moved school → 301 to brand "find a school" page with HTTP 200 | Validate: final URL contains school #/city slug; title contains school identity; else flag `REDIRECTED`, don't cache as valid |
| EC | Soft 404 / "temporarily closed" / maintenance page | Min-length + marker checks before caching; quarantine cache dir for invalid fetches |
| EC | Cookie/geo consent overlays injected into DOM | Strip known consent containers; they're why "near you"/"affordable" false-positive |
| EC | Slow hydration — `domcontentloaded` + fixed 1.5s may precede content | Wait for brand-profile selector (e.g. `.welcomeMessage`) with timeout, fallback `networkidle` |
| BP | Crash mid-run | `gather(return_exceptions=True)` + per-school try/except + run manifest; report generates from whatever completed |

### Stage 2 — Extraction
| Type | Case | Handling |
|---|---|---|
| EC ✅ | 12 domains ≈ 12 templates; Montessori (28) likely very different | Per-brand selector profiles from golden calibration; generic heading-walk fallback; `extraction_method` recorded per school |
| EC | No "Welcome" heading at all | `welcome_found=false` → manual-review queue; never fake-fallback (defect #7) |
| EC | Multiple "welcome" headings (hero + section + classroom pages) | Prefer exact "Welcome to Our School", then brand-profile position; log all candidates |
| EC | Container too large/small (heading-only div, or page-wrapper div) | Reject container if < 40 words or > 60% of page text; walk up/down accordingly |
| EC ✅ | Carousel/slider block duplication (3× "Meet Our Staff") | De-duplicate identical text blocks before counts |
| EC | Address/city only in footer or JSON-LD, not visible copy | Decision: audit scores **visible body copy** (it's a copywriting audit); JSON-LD/footer recorded as separate diagnostic columns so analysts see "present but not in copy" |
| EC | Spanish-language or bilingual pages | Detect language; flag for manual review (keyword lists are English) |
| EC | Smart quotes, `&amp;`, non-breaking spaces, mojibake ✅ ("Denny’s") | Unicode normalization (NFKC) + quote folding before all matching |

### Stage 3 — Deterministic evaluation
| Type | Case | Handling |
|---|---|---|
| BP ✅ | 2-letter state substring bug (defect #2) | `\b` word-boundary regex; for states also accept full name; require comma/space context ("Jacksonville, FL") to avoid "FL" in unrelated caps text |
| EC ✅ | Street written differently than ground truth ("Beach Blvd" vs "Beach Boulevard") | Suffix/directional expansion dictionary (USPS pub 28 abbreviations); match street-name token |
| EC | City name inside other words or other cities ("Charlotte" in "Port Charlotte") | Word-boundary + optional state-context bonus |
| EC | Sibling-school addresses in "nearby schools" widget | Body-scope stripping + require match of *this* school's street token |
| EC | Hyphenated/apostrophe city names (Winston-Salem, O'Fallon, St. Paul/Saint Paul) | Normalization layer |
| EC | `pre-k` vs `Pre-Kindergarten` vs `PreK` | Variant regex set; count as same head keyword |
| EC | Keyword inside testimonial or negation ("not just a daycare") | Semantic context pass (Q5) decides PRESENT vs PARTIAL |
| EC | School name partial ("La Petite Academy" without "on Beach Blvd.") | Fuzzy threshold: brand+anchor-token = PRESENT, brand-only = PARTIAL |

### Stage 4 — Semantic (LLM) evaluation
| Type | Case | Handling |
|---|---|---|
| BP | Evidence hallucination (snippet not on page) | Verbatim snippet verification in Python; downgrade + flag (Part 4.3) |
| EC | Landmark vs. generic geography ("in Florida" is not a landmark) | SKILL rule: must be a named place/business/road-relation at sub-city scale |
| EC | GEO_06 vs GEO_07 assignment ambiguity (which landmark is "1"?) | First-mentioned in Welcome = Landmark 1; order recorded |
| BP | Invalid JSON / schema drift / truncation on long pages | Structured outputs + retry ×2 + `semantic_failed` flag; cap body_text at ~3k words with head+tail sampling |
| EC | Rate limits / API outage mid-run | Bounded concurrency on LLM calls, backoff, resume from manifest (per-school persistence already isolates loss) |
| BP | Rubric drift between calibration and full run | `config_hash` + `model_id` pinned in observations; full run refuses to mix hashes |
| EC | Confidence calibration | Report mean confidence but gate on evidence verification, not confidence |

### Stage 5 — Scoring
| Type | Case | Handling |
|---|---|---|
| BP | "Not offered" vs "not mentioned" indistinguishable (INC_07 bus service, LEAD_01 no director listed) | Documented limitation: audit measures *mention*, not *offering*. N/A only where ground truth proves inapplicability (e.g., GEO_04 for the 730 city-fallback + 29 no-data schools; Montessori Compass only for MU) |
| EC | Weight conflict: spec says Must-Have→4.0 but config has GEO_01 must-have at weight 1.5 | **Decision needed** (Part 7, D1) |
| EC | Optional params (GEO_07) drag scores when absent | Keep in denominator per spec, but publish per-category sub-scores so the effect is visible |
| EC | `welcome_found=false` schools | S_welcome=0 with reason column, excluded from averages-by-default in summary sheet (separate cohort) |
| EC | Boilerplate inflation: brand template supplies INC_*/THM_* text identically on all pages of a brand | Compute brand-level "template baseline" (params present on >90% of a brand's pages with near-identical snippets) and add a `Local_Uniqueness` diagnostic column, so client sees template-driven vs. locally-written coverage |

### Stage 6 — Reporting
| Type | Case | Handling |
|---|---|---|
| BP | openpyxl `IllegalCharacterError` on scraped control characters | Sanitize all strings; truncate cells >32,000 chars |
| EC | Formula injection (snippet starts with `=`, `+`, `-`, `@`) | Prefix with `'` |
| EC | 62,328-row matrix sheet (not 37,400 — count corrected) | Fine for xlsx; freeze panes + autofilter; avoid per-cell styling loops that would take minutes |
| EC | Snippets containing emoji/RTL/unusual unicode | Keep as-is after sanitization; Calibri handles most |
| EC | Rank ties | Stable sort: S_welcome desc, then Must-Have desc, then School # |

### Ops & cost
| Type | Case | Handling |
|---|---|---|
| BP | Re-running semantic eval on all 1,113 after every SKILL tweak | That's exactly what the extraction cache + `--eval-only` on the 10-school golden set avoids; full-network re-eval only after calibration sign-off |
| EC | HTML snapshots go stale vs. report date | Record fetch date per school; report header carries snapshot window |
| EC | Disk: ~1,113 × ~180KB HTML ≈ 200MB + JSON | Trivial; keep out of git (.gitignore `data/raw_html`) |

---

## Part 7 — Scoring Engine & Open Decisions

Weights: Must-Have 4.0 · High 3.0 · Medium 1.5 · Low 1.0 — Status values: PRESENT 1.0 · PARTIAL 0.5 · ABSENT 0.0 · N/A excluded from numerator **and** denominator.

```
S_welcome  = Σ(Wᵢ·Vᵢ,welcome) / Σ(Wᵢ over applicable params) × 100
S_page     = Σ(Wᵢ·Vᵢ,page)    / Σ(Wᵢ over applicable params) × 100
S_musthave = same, restricted to must-have params (welcome scope)
S_high     = same, restricted to High priority (welcome scope)
S_gap      = S_page − S_welcome
Confidence = mean of evidence-verified semantic confidences (diagnostic only)
```

**Decisions needed from you / the SEO team before Phase 2 sign-off:**

- **D1 — DECIDED (2026-10-05):** must-have forces W=4.0 regardless of priority; applied to `config/parameters_database.json` (GEO_01–GEO_06, STR_02 reweighted; 11 must-have parameters total).
- **D2 — DEFERRED (2026-10-05):** Childcare Network decision to come from client later; current scope = the 1,113 RIO schools.
- **D3 — Footer/JSON-LD presence:** confirm "visible body copy only" scoring with separate diagnostic columns (recommended), or count any on-page occurrence.
- **D4 — Duplicate-URL school pairs:** report as two rows (same scores) or one merged row?
- **D5 — PARTIAL definitions per parameter class** (e.g., brand-only name match, paraphrase-only keyword; note: NEARBY-neighborhood mentions earn no credit at all per ARCHITECTURE.md §3) — needs analyst sign-off during golden calibration.

---

## Part 8 — Cost & Runtime Model (full 1,113-school run)

| Stage | Volume | Estimate |
|---|---|---|
| Fetch (Playwright, shared browser, concurrency 5, per-domain throttle) | 1,113 pages | ~45–75 min incl. retries |
| Extraction + deterministic | 1,113 | < 2 min (CPU) |
| Semantic LLM (1 call/school, ~4–6k in / ~1.5–2k out tokens, rubric prompt-cached) | 1,113 calls | ~30–45 min at concurrency 5 |
| LLM cost @ default `claude-opus-5-5` ($4/M in, $20/M out) | ~6M in / ~2M out | **~$60–70 per full run** (≈ half that via the Batch API if latency doesn't matter; `claude-sonnet-5-5` ≈ $30–35; Haiku-class ≈ $12–18; 10-school calibration: pennies). Model set via `LCG_LLM_MODEL` |
| Report generation | 62,328-row matrix | ~1–2 min |

Re-runs after rubric changes cost **$0 in scraping** (HTML cached) and only the LLM delta.

---

## Part 9 — Roadmap (revised to reflect verified reality)

**Phase 0 — Data consolidation (NEW, prerequisite):**
1. Generate `data/master_schools.csv` from `RIO_Location_Export.xlsx` (1,113 rows; normalize ZIPs; resolve brand `None`; flag the 2 duplicate-URL pairs; add brand-alias column). Replace the current fabricated 2-row placeholder.
2. Consolidate 51 loc-intel batch workbooks → `data/neighborhood_lookup.json` (assert ≥1,112 schools; carry relationship/confidence/SEO-flag; mark the 174 no-neighborhood schools N/A).
3. Fix `config/brand_defaults.json` keys to RIO brand strings; detection term `SproutAbout` (no ®).
4. Resolve D1 weight rule in `parameters_database.json`; standardize docs on **56 parameters**.

**Phase 1 — Spec freeze:** SKILL.md rewritten with per-parameter PRESENT/PARTIAL/ABSENT rules, JSON schema, evidence-verbatim requirement; config hash versioning.

**Phase 2 — Golden calibration (10 schools: 3 strong / 3 average / 3 sparse / 1 edge — include 1 Montessori, 1 Gilden Woods, 1 no-welcome page if found):** build brand selector profiles; analyst grades vs. pipeline output; iterate SKILL until ≥95% agreement; sign off PARTIAL definitions (D5).

**Phase 3 — Core hardening:** fix all Part 5 defects (word-boundary matcher, real LLM evaluator, shared browser + fetch validation, nav/footer stripping, welcome_found flag, return_exceptions + run manifest, Excel sanitization, per-domain throttle, N/A support, config-hash cache invalidation).

**Phase 4 — Full network run:** batch over 1,113 with failure manifest; QA sample (30 random schools re-checked by analyst); deliver `LCG_Welcome_SEO_Audit.xlsx` + run-summary sheet (fetch failures, redirects, low-confidence, snapshot window).

**Verification plan:** extractor unit test against cached 7440 snapshot (known-good: welcome found, 230±10 words, `div.welcomeMessage` path) · deterministic engine tests on synthetic snippets incl. the FL-substring trap and Blvd/Boulevard case · scoring math tests vs. hand-calculated fixtures · end-to-end golden-set comparison gate before Phase 4.

---

## Part 10 — Running It

```bash
pip install -r requirements.txt
python -m playwright install chromium        # required — requests fallback does NOT work against these sites

python main.py                               # full run from data/master_schools.csv
python main.py --csv test_golden_dataset/10_schools_sample.csv   # calibration batch
python main.py --concurrency 5 --output LCG_Welcome_SEO_Audit.xlsx
python main.py --force                       # ignore caches, refetch + re-evaluate
# proposed (Phase 3): --fetch-only | --eval-only | --rescore | --report-only
```

**Environment:** Python 3.11 · key deps: playwright, beautifulsoup4, lxml, pandas, openpyxl, pydantic · LLM API key via `.env` (never committed).

**Output workbook:** `00_Run_Summary` (proposed) · `01_School_Scorecard` · `02_Parameter_Presence_Matrix` (62,328 rows) · `03_Parameter_Summary` (MOVE vs CREATE counts per parameter) · `04_Remediation_Queue` (copywriter task list) · `05_Evaluation_Evidence` (DOM paths, evidence, confidence, fetch dates).
