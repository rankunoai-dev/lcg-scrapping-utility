"""Text normalization primitives shared by ingest and matching modules.

Pure functions only — no I/O, no state. The S2 build step extends this module
with the word-boundary matchers and USPS street-suffix dictionaries; the
functions below are the subset S1 (ingest) needs, written to their final
contracts so S2 only adds, never changes.

Known traps these functions must survive (verified against live data):
- Curly apostrophes:        "Denny's"  vs "Denny’s"
- Apostrophe-less brands:   "The Childrens Courtyard" (RIO) vs "The Children's Courtyard" (site)
- Saint/St folding:         "St. Paul" vs "saint-paul" (URL slug)
- Hyphenated cities:        "Winston-Salem", "Land O' Lakes" slug "land-o-lakes"
"""

from __future__ import annotations

import re
import unicodedata

# Every unicode apostrophe/quote variant we fold to a plain ASCII apostrophe.
_QUOTE_RX = re.compile(r"[‘’‚ʼ`´]")
_WS_RX = re.compile(r"\s+")
_NON_ALNUM_RX = re.compile(r"[^a-z0-9]+")
_SAINT_RX = re.compile(r"\bsaint\b")


def fold_text(s: str) -> str:
    """Unicode-normalize (NFKC), fold quote variants to ', collapse whitespace.

    Case is preserved; use for evidence snippets and display strings.
    """
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = _QUOTE_RX.sub("'", s)
    return _WS_RX.sub(" ", s).strip()


def norm_key(s: str) -> str:
    """Aggressive canonical key for equality comparison of names/places.

    Lowercase, apostrophes removed, saint->st, every non-alphanumeric run
    becomes a single space. "Land O' Lakes" == "land-o-lakes" == "Land O Lakes".
    """
    s = fold_text(s).lower().replace("'", "")
    s = _NON_ALNUM_RX.sub(" ", s).strip()
    s = _SAINT_RX.sub("st", s)
    return _WS_RX.sub(" ", s).strip()


def slug_to_key(slug: str) -> str:
    """URL slug fragment ('fort-worth', 'land-o-lakes') -> comparison key."""
    return norm_key(slug.replace("-", " "))


def norm_zip(value) -> str:
    """Post Code of any type -> 5-char zero-padded string ('' if empty).

    ZIP+4 inputs are truncated to the 5-digit prefix.
    """
    if value is None:
        return ""
    s = str(value).strip().replace(" ", "")
    if not s:
        return ""
    s = s.split("-")[0].split(".")[0]  # ZIP+4 and float-typed cells
    if s.isdigit():
        return s.zfill(5)[:5] if len(s) <= 5 else s[:5]
    return s


def norm_url(url: str) -> str:
    """Comparison form of a URL: lowercase scheme/host, no trailing slash."""
    u = fold_text(url)
    m = re.match(r"(?i)^(https?)://([^/]+)(/.*)?$", u)
    if not m:
        return u.lower()
    scheme, host, path = m.group(1).lower(), m.group(2).lower(), m.group(3) or ""
    if host.startswith("www."):
        host = host[4:]
    return f"{scheme}://{host}{path.rstrip('/')}"


# ---------------------------------------------------------------------------
# S2: dictionaries + flexible word-boundary matching
# ---------------------------------------------------------------------------

# USPS Publication 28 street suffixes (common subset) grouped by class:
# any variant in a class matches any other ("Beach Blvd" == "Beach Boulevard").
SUFFIX_CLASSES: dict[str, list[str]] = {
    "ave":  ["ave", "av", "avenue"],
    "blvd": ["blvd", "boul", "boulevard"],
    "cir":  ["cir", "circle"],
    "ct":   ["ct", "court"],
    "dr":   ["dr", "drive"],
    "expy": ["expy", "expwy", "expressway"],
    "fwy":  ["fwy", "freeway"],
    "hwy":  ["hwy", "highway"],
    "ln":   ["ln", "lane"],
    "loop": ["loop"],
    "pike": ["pike"],
    "pkwy": ["pkwy", "pky", "parkway"],
    "pl":   ["pl", "place"],
    "plz":  ["plz", "plaza"],
    "rd":   ["rd", "road"],
    "sq":   ["sq", "square"],
    "st":   ["st", "str", "street"],
    "ter":  ["ter", "terrace"],
    "tpke": ["tpke", "tpk", "turnpike"],
    "trl":  ["trl", "trail"],
    "way":  ["way"],
}
# variant -> class key ("boulevard" -> "blvd")
SUFFIX_LOOKUP: dict[str, str] = {
    v: cls for cls, variants in SUFFIX_CLASSES.items() for v in variants
}

DIRECTIONAL_CLASSES: dict[str, list[str]] = {
    "n": ["n", "north"], "s": ["s", "south"], "e": ["e", "east"], "w": ["w", "west"],
    "ne": ["ne", "northeast"], "nw": ["nw", "northwest"],
    "se": ["se", "southeast"], "sw": ["sw", "southwest"],
}
DIRECTIONAL_LOOKUP: dict[str, str] = {
    v: cls for cls, variants in DIRECTIONAL_CLASSES.items() for v in variants
}

# State abbreviation -> full name (50 states + DC).
STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# Compound city prefixes: if the word right before a city match is one of these
# and prefix+city is NOT the school's own city, the mention is a different
# place ("Port Charlotte" is not "Charlotte") and earns no credit.
COMPOUND_CITY_PREFIXES = {
    "port", "north", "south", "east", "west", "new", "old", "fort", "ft",
    "mount", "mt", "lake", "saint", "st", "cape", "san", "santa", "el",
    "las", "los", "little", "grand", "upper", "lower",
}

# Word tokens: letters/digits with optional internal apostrophes ("denny's").
_TOKEN_SPLIT_RX = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")
# Separator allowed between tokens of a phrase in page text.
_SEP = r"[\s\-./]+"
# Custom boundaries: apostrophes must not break them ("Denny's" end is fine).
_LB = r"(?<![A-Za-z0-9])"
_RB = r"(?![A-Za-z0-9])"


def tokens_of(term: str) -> list[str]:
    """Lowercased alphanumeric tokens with apostrophes removed.

    "O'Fallon" -> ["ofallon"]; "St. Paul" -> ["st", "paul"]
    """
    folded = fold_text(term).lower()
    return [t.replace("'", "") for t in _TOKEN_SPLIT_RX.findall(folded)]


def _token_rx(token: str) -> str:
    """Regex for one token tolerating internal apostrophes in the page text.

    "childrens" matches both "Childrens" and "Children's";
    "dennys" matches "Denny's".
    """
    return "'?".join(re.escape(ch) for ch in token)


def _expanded_token_rx(token: str, expansions: dict[str, list[str]] | None) -> str:
    variants = expansions.get(token) if expansions else None
    if not variants:
        return _token_rx(token)
    return "(?:" + "|".join(_token_rx(v) for v in sorted(set(variants), key=len, reverse=True)) + ")"


def phrase_pattern(term: str, expansions: dict[str, list[str]] | None = None) -> re.Pattern:
    """Word-boundary, separator- and apostrophe-tolerant pattern for a phrase.

    Survives the verified traps: "FL" never matches inside "flexible"
    (boundaries), "Winston-Salem" matches "Winston Salem", "Denny's" matches
    the curly-quoted original (fold the text first with `fold_text`).
    `expansions` maps a normalized token to acceptable alternatives, e.g.
    {"st": ["st", "saint"]} or a street-suffix class.
    """
    toks = tokens_of(term)
    if not toks:
        return re.compile(r"(?!x)x")  # matches nothing
    body = _SEP.join(_expanded_token_rx(t, expansions) for t in toks)
    return re.compile(_LB + body + _RB, re.IGNORECASE)


def snippet_around(text: str, start: int, end: int, padding: int = 60) -> str:
    s, e = max(0, start - padding), min(len(text), end + padding)
    prefix = "..." if s > 0 else ""
    suffix = "..." if e < len(text) else ""
    return f"{prefix}{text[s:e].strip()}{suffix}"


def flex_search(term_or_variants, text: str,
                expansions: dict[str, list[str]] | None = None):
    """Search folded `text` for a term (or first of several variants).

    Returns (matched_variant, re.Match) or (None, None). `text` should already
    be `fold_text`-ed by the caller so evidence offsets refer to it directly.
    """
    variants = [term_or_variants] if isinstance(term_or_variants, str) else list(term_or_variants)
    for v in variants:
        m = phrase_pattern(v, expansions).search(text)
        if m:
            return v, m
    return None, None
