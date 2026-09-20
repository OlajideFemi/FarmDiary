# %% [markdown]
# # Government Grants Multi-Registry Entity Resolution — V2.3
#
# Production-oriented, end-to-end pipeline for enriching Government Grants
# records with Companies House numbers while preserving every original input
# row and column.
#
# V2.3 keeps the proven Companies House search and deterministic searches
# across the Charity Commission for England and Wales, OSCR and CCNI. It adds:
#
# - full input-row preservation (matching is performed on a separate entity table);
# - strict and loose name representations;
# - registered-name and trading-name aliases for `T/A` / `Trading As` records;
# - public-body, education, charity, company and uncertain routing;
# - true Jaro–Winkler and normalized Levenshtein similarities;
# - truncation-aware prefix containment;
# - chunked, resumable Companies House scanning;
# - preflight postcode, row-alignment and reference-file QA;
# - current plus ten historical Companies House names;
# - deterministic decisions and an auditable evidence score (not a probability);
# - entity-aware routing for public, charity, education, international and
#   likely sole-trader/person records;
# - jurisdiction-aware charity ranking for cross-border registrations;
# - guarded Companies House-to-charity company-number crosswalks;
# - optional Charity Commission historical/other-name aliases;
# - verified long-prefix charity blocking;
# - conservative person and public-body routing corrections;
# - cap-saturation reporting and honestly named Top-* audit files;
# - versioned charity-registry caching;
# - selected identifiers only for deterministic matches, with candidate fields
#   kept separate for human review;
# - one QA workbook plus compressed Companies House and charity audit files.
#
# Edit only the configuration cell, then run all cells.

# %%
# Run once if required:
# %pip install pandas numpy rapidfuzz openpyxl

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
try:
    from rapidfuzz import fuzz
    from rapidfuzz.distance import JaroWinkler, Levenshtein
except ImportError:  # The notebook remains runnable before optional acceleration.
    fuzz = None
    JaroWinkler = None
    Levenshtein = None


LOGGER = logging.getLogger("entity_resolution_v2")
if not LOGGER.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


# %% [markdown]
# ## 1. Configuration
#
# The defaults match the folder and filenames used in the V1 notebook.
# Candidate caps bound audit-file size. V2.3 records every saturated entity in
# the QA workbook so the files are explicitly "Top" candidates, not exhaustive.

# %%
@dataclass(frozen=True)
class PipelineConfig:
    base_folder: Path = Path(
        r"C:/Users/Oluwafemi/Documents/03_Sandbox/Grants_Analysis"
    )
    grants_filename: str = "Grant_Recipients_2.csv"
    companies_house_filename: str = "BasicCompanyDataAsOneFile-2026-09-01.csv"
    reference_grants_filename: str | None = "Grant_Recipients_1.csv"
    charity_ew_filename: str = "CharityExport/publicextract.charity.zip"
    charity_ew_other_names_filename: str = (
        "CharityExport/publicextract.charity_other_names.zip"
    )
    charity_scotland_filename: str = "CharityExport/CharityExport-19-Sep-2026.zip"
    charity_ni_filename: str = "CharityExport/charitydetails_2026_09_20_01_10_06.csv"
    output_subfolder: str = "Entity_Resolution_Output_V2_3"

    grant_name_col: str = "Name"
    grant_postcode_col: str = "Post Code"

    chunk_size: int = 100_000
    max_candidates_per_entity: int = 300
    min_cheap_name_score: float = 0.50
    diagnostics_top_n: int = 5
    max_charity_candidates_per_entity: int = 150
    crosswalk_candidate_rank_limit: int = 100
    crosswalk_auto_rank_limit: int = 5

    review_min_name_score: float = 0.78
    strong_review_name_score: float = 0.88
    exact_name_min_distinctive_tokens: int = 2
    stop_on_alignment_error: bool = True

    suspected_source_name_limit: int = 35
    reuse_candidate_cache: bool = True
    reuse_v22_ch_cache: bool = True
    force_rescan: bool = False
    use_charity_registries: bool = True

    @property
    def grants_file(self) -> Path:
        return self.base_folder / self.grants_filename

    @property
    def companies_house_file(self) -> Path:
        return self.base_folder / self.companies_house_filename

    @property
    def output_folder(self) -> Path:
        return self.base_folder / self.output_subfolder

    @property
    def charity_ew_file(self) -> Path:
        return self.base_folder / self.charity_ew_filename

    @property
    def charity_ew_other_names_file(self) -> Path:
        return self.base_folder / self.charity_ew_other_names_filename

    @property
    def charity_scotland_file(self) -> Path:
        return self.base_folder / self.charity_scotland_filename

    @property
    def charity_ni_file(self) -> Path:
        return self.base_folder / self.charity_ni_filename

    @property
    def workbook_file(self) -> Path:
        return self.output_folder / "Entity_Resolution_V2_3_QA.xlsx"

    @property
    def all_candidates_file(self) -> Path:
        return self.output_folder / "Top_CH_Candidates_V2_3.csv.gz"

    @property
    def all_charity_candidates_file(self) -> Path:
        return self.output_folder / "Top_Charity_Candidates_V2_3.csv.gz"

    @property
    def candidate_cache_file(self) -> Path:
        return self.output_folder / "CH_Candidate_Pool_V2_3.pkl"

    @property
    def candidate_cache_meta_file(self) -> Path:
        return self.output_folder / "CH_Candidate_Pool_V2_3.meta.json"

    @property
    def v22_candidate_cache_file(self) -> Path:
        return (
            self.base_folder
            / "Entity_Resolution_Output_V2_2"
            / "CH_Candidate_Pool_V2_2.pkl"
        )

    @property
    def v22_candidate_cache_meta_file(self) -> Path:
        return (
            self.base_folder
            / "Entity_Resolution_Output_V2_2"
            / "CH_Candidate_Pool_V2_2.meta.json"
        )

    @property
    def charity_cache_file(self) -> Path:
        return self.output_folder / "Charity_Registry_V2_3.pkl"

    @property
    def charity_cache_meta_file(self) -> Path:
        return self.output_folder / "Charity_Registry_V2_3.meta.json"

    @property
    def reference_grants_file(self) -> Path | None:
        if not self.reference_grants_filename:
            return None
        return self.base_folder / self.reference_grants_filename

    @property
    def preflight_file(self) -> Path:
        return self.output_folder / "Input_QA_Preflight_V2_3.xlsx"


CONFIG = PipelineConfig()
CONFIG


# %% [markdown]
# ## 2. Name, postcode, alias and entity-routing functions

# %%
LEGAL_FORM_PATTERNS = {
    "LTD": r"\b(?:LIMITED|LIMTED|LTD|CYFYNGEDIG)\b",
    "PLC": r"\b(?:PUBLIC LIMITED COMPANY|PLC)\b",
    "LLP": r"\b(?:LIMITED LIABILITY PARTNERSHIP|LLP)\b",
    "CIC": r"\b(?:COMMUNITY INTEREST COMPANY|CIC)\b",
    "INC": r"\b(?:INCORPORATED|INC)\b",
    "CORP": r"\b(?:CORPORATION|CORP)\b",
    "SPA": r"\bS\s*P\s*A\b",
    "SE": r"\bSOCIETAS EUROPAEA\b",
}

LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:LIMITED|LIMTED|LTD|CYFYNGEDIG|PLC|LLP|CIC|INCORPORATED|INC|CORPORATION|CORP)\b",
    flags=re.IGNORECASE,
)

BLOCK_STOPWORDS = {
    "THE", "A", "AN", "OF", "AND", "FOR", "IN", "AT", "TO", "T", "TA"
}

TRADING_AS_RE = re.compile(
    r"\s+(?:T\s*/?\s*A|TRADING\s+AS)\b\s*",
    flags=re.IGNORECASE,
)

PUBLIC_BODY_PATTERNS = [
    r"\b(?:MBC|BC|DC|CC)\b",
    r"\b(?:COUNCIL|BOROUGH|COUNTY COUNCIL|DISTRICT COUNCIL)\b",
    r"\bCOMBINED(?: AUTHORITY)?\b",
    r"\bMAYORAL\b",
    r"\bCITY REGION\b",
    r"\bTRANSPORT FOR (?:LONDON|NORTH|NORTH EAST|THE NORTH)\b",
    r"\b(?:NHS|POLICE|FIRE AND RESCUE|GOVERNMENT DEPARTMENT)\b",
    r"\bOFFICE OF RAIL AND ROAD(?:S)?\b",
    r"\b(?:COMBINED|LOCAL|PORT|HARBOUR|TRANSPORT) AUTHORITY\b",
    r"\b(?:HARBOUR|PORT) BOARD\b",
    r"\bNATIONAL PARK(?:S)?(?: AUTHORITY)?\b",
    r"\bDEPARTMENT (?:FOR|OF)\b",
    r"\bMINISTRY OF\b",
    r"\bSTATUTORY BODY\b",
    r"\bCITY OF .+\b(?:SOCIAL SERVICES|COMMUNITY SERVICES|COM(?: AND)? SOC SERV)\b",
    r"^NORTH YORK MOORS(?: NATIONAL PARK(?: AUTHORITY)?)?$",
    r"\bKCC\b",
]

EDUCATION_PATTERNS = [
    r"\b(?:ACADEMY|SCHOOL|COLLEGE|UNIVERSITY|EDUCATION|MULTI ACADEMY)\b",
]

CHARITY_PATTERNS = [
    r"\b(?:CHARITY|CHARITABLE|FOUNDATION|CIO)\b",
]

INTERNATIONAL_PATTERNS = [
    r"\bOECD\b",
    r"\bECAC\b",
    r"\bEUROPEAN CIVIL AVIATION\b",
    r"\bINTERNATIONAL CIVIL AVIATION\b",
    r"\bS\s*P\s*A\b",
    r"\bSOCIETAS EUROPAEA\b",
    r"\b(?:GMBH|SARL|SAS|BV|NV|AKTIEBOLAG|OYJ)\b",
]

BUSINESS_NAME_TERMS = {
    "ELECTRIC", "ELECTRICAL", "ELECTRICS", "ENERGY", "SERVICES", "SERVICE",
    "SOLUTIONS", "ENGINEERING", "TRANSPORT", "MOTORS", "MOTOR", "WORKSHOP",
    "GROUP", "FOUNDATION", "ASSOCIATION", "ESTATES", "CYCLING", "CHARGING",
    "RENEWABLE", "RENEWABLES", "SECURITY", "HEATING", "INSTALLATION",
    "UK", "GB", "HOSPITAL", "HOSPITALS", "HEALTH", "CARE", "TRUST",
    "SOCIETY", "CLUB", "CHAMBER", "COMMERCE", "UNION", "FEDERATION",
    "AUTOEXCHANGE", "AUTOMOTIVE", "GARAGE", "MOTORING", "PARTS",
    "ELECTRICIAN", "ELECTRICIANS", "PLUMBER", "PLUMBERS", "BUILDER",
    "BUILDERS", "JOINER", "JOINERS", "ROOFER", "ROOFERS",
    "MANAGEMENT", "CONSULTING", "CONSULTANCY", "PARTNERS", "ASSOCIATES",
    "HOLDINGS", "VENTURES", "CAPITAL", "INVESTMENTS", "PROPERTIES",
    "LOGISTICS", "DISTRIBUTION", "SUPPLY", "WHOLESALE", "RETAIL",
    "MEDIA", "PUBLISHING", "COMMUNICATIONS", "TECHNOLOGIES", "SYSTEMS",
    "LABORATORIES", "RESEARCH", "INSTITUTE", "ACADEMY",
}

DISTINCTIVE_STOPWORDS = BLOCK_STOPWORDS | {
    "LTD", "LIMITED", "PLC", "LLP", "CIC", "UK", "GROUP", "COMPANY", "CO",
    "SERVICES", "SERVICE", "SOLUTIONS", "ELECTRIC", "ELECTRICAL", "ELECTRICS",
    "CONTRACTOR", "CONTRACTORS", "ENGINEERING", "INSTALLATION", "ENTERPRISE",
    "ENTERPRISES", "TRADING", "TA",
}

UK_POSTCODE_AREAS = {
    "AB", "AL", "B", "BA", "BB", "BD", "BH", "BL", "BN", "BR", "BS", "BT",
    "CA", "CB", "CF", "CH", "CM", "CO", "CR", "CT", "CV", "CW", "DA", "DD",
    "DE", "DG", "DH", "DL", "DN", "DT", "DY", "E", "EC", "EH", "EN", "EX",
    "FK", "FY", "G", "GL", "GU", "GY", "HA", "HD", "HG", "HP", "HR", "HS",
    "HU", "HX", "IG", "IM", "IP", "IV", "JE", "KA", "KT", "KW", "KY", "L",
    "LA", "LD", "LE", "LL", "LN", "LS", "LU", "M", "ME", "MK", "ML", "N",
    "NE", "NG", "NN", "NP", "NR", "NW", "OL", "OX", "PA", "PE", "PH", "PL",
    "PO", "PR", "RG", "RH", "RM", "S", "SA", "SE", "SG", "SK", "SL", "SM",
    "SN", "SO", "SP", "SR", "SS", "ST", "SW", "SY", "TA", "TD", "TF", "TN",
    "TQ", "TR", "TS", "TW", "UB", "W", "WA", "WC", "WD", "WF", "WN", "WR",
    "WS", "WV", "YO", "ZE",
}

UK_POSTCODE_RE = re.compile(
    r"^(?:GIR0AA|(?:[A-Z][0-9]{1,2}|[A-Z][A-Z][0-9]{1,2}|"
    r"[A-Z][0-9][A-Z]|[A-Z][A-Z][0-9][A-Z])[0-9][A-Z]{2})$"
)

UK_POSTCODE_SEARCH_RE = re.compile(
    r"\b(?:GIR\s?0AA|(?:[A-Z][0-9]{1,2}|[A-Z][A-Z][0-9]{1,2}|"
    r"[A-Z][0-9][A-Z]|[A-Z][A-Z][0-9][A-Z])\s?[0-9][A-Z]{2})\b",
    flags=re.IGNORECASE,
)

SCOTTISH_POSTCODE_AREAS = {
    "AB", "DD", "DG", "EH", "FK", "G", "HS", "IV", "KA", "KW", "KY",
    "ML", "PA", "PH", "TD", "ZE",
}

COMPASS_EXPANSIONS = {
    "NW": "NORTH WEST",
    "NE": "NORTH EAST",
    "SW": "SOUTH WEST",
    "SE": "SOUTH EAST",
}


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def normalise_name(value: Any) -> str:
    """Strict representation: punctuation-normalised, legal form retained."""
    x = _text(value).upper()
    x = re.sub(r"[’`´]", "'", x)
    x = re.sub(r"'S\b", "", x)
    x = x.replace("&", " AND ")
    x = re.sub(r"\bLIMTED\b", "LIMITED", x)
    x = re.sub(r"\bLIMITED\b", "LTD", x)
    x = re.sub(r"\bPUBLIC LIMITED COMPANY\b", "PLC", x)
    x = re.sub(r"\bLIMITED LIABILITY PARTNERSHIP\b", "LLP", x)
    x = re.sub(r"\bCOMMUNITY INTEREST COMPANY\b", "CIC", x)
    x = re.sub(r"[^A-Z0-9\s]", " ", x)
    return re.sub(r"\s+", " ", x).strip()


def loose_name(value: Any) -> str:
    """Loose representation: strict representation without legal suffixes."""
    x = normalise_name(value)
    x = LEGAL_SUFFIX_RE.sub(" ", x)
    return re.sub(r"\s+", " ", x).strip()


def controlled_name(value: Any) -> str:
    """Loose name plus only explicitly approved abbreviation expansions."""
    tokens = loose_name(value).split()
    expanded: list[str] = []
    for token in tokens:
        expanded.extend(COMPASS_EXPANSIONS.get(token, token).split())
    return " ".join(expanded)


def compact_name(value: Any) -> str:
    """Controlled representation without spaces, useful for SGS/S G S and GO2/GO 2."""
    return controlled_name(value).replace(" ", "")


def distinctive_tokens(value: Any) -> set[str]:
    return {
        token for token in controlled_name(value).split()
        if token not in DISTINCTIVE_STOPWORDS and len(token) >= 2
    }


def extract_legal_form(value: Any) -> str:
    x = normalise_name(value)
    for legal_form, pattern in LEGAL_FORM_PATTERNS.items():
        if re.search(pattern, x):
            return legal_form
    return ""


def normalise_postcode(value: Any) -> str:
    return re.sub(r"\s+", "", _text(value).upper())


def postcode_sector(value: Any) -> str:
    # RM12 3YT -> RM123; the final two delivery-point letters are removed.
    postcode = normalise_postcode(value)
    return postcode[:-2] if len(postcode) >= 3 else ""


def postcode_area(value: Any) -> str:
    match = re.match(r"^[A-Z]{1,2}", normalise_postcode(value))
    return match.group(0) if match else ""


def is_valid_uk_postcode(value: Any) -> bool:
    postcode = normalise_postcode(value)
    if not postcode or not UK_POSTCODE_RE.fullmatch(postcode):
        return False
    return postcode_area(postcode) in UK_POSTCODE_AREAS


def extract_uk_postcode(value: Any) -> str:
    """Extract the last valid UK postcode embedded in an address."""
    matches = UK_POSTCODE_SEARCH_RE.findall(_text(value).upper())
    for match in reversed(matches):
        postcode = normalise_postcode(match)
        if is_valid_uk_postcode(postcode):
            return postcode
    return ""


def postcode_jurisdiction(value: Any) -> str:
    """Return the most likely home charity regulator for a UK postcode."""
    area = postcode_area(value)
    if area == "BT":
        return "NORTHERN_IRELAND"
    if area in SCOTTISH_POSTCODE_AREAS:
        return "SCOTLAND"
    if is_valid_uk_postcode(value):
        return "ENGLAND_WALES"
    return "UNKNOWN"


def split_name_aliases(value: Any) -> list[tuple[str, str]]:
    """Return whole, registered-name and trading-name aliases without guessing."""
    raw = _text(value)
    if not raw:
        return []

    aliases: list[tuple[str, str]] = [("WHOLE_NAME", raw)]
    parts = TRADING_AS_RE.split(raw, maxsplit=1)
    if len(parts) == 2:
        registered, trading = (part.strip(" -") for part in parts)
        if registered:
            aliases.append(("REGISTERED_NAME", registered))
        if trading:
            aliases.append(("TRADING_NAME", trading))

    # When a legal form appears before a trailing branch/geography qualifier,
    # also compare the legal core. Example: HSBC BANK PLC LONDON.
    strict = normalise_name(raw)
    legal_match = re.search(r"\b(?:LTD|PLC|LLP|CIC)\b", strict)
    if legal_match and legal_match.end() < len(strict):
        aliases.append(("LEGAL_CORE", strict[: legal_match.end()]))

    # Preserve order while removing duplicate normalized aliases.
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for role, alias in aliases:
        key = normalise_name(alias)
        if key and key not in seen:
            output.append((role, alias))
            seen.add(key)
    return output


def _looks_like_person_or_sole_trader(value: Any) -> bool:
    raw = _text(value)
    strict = normalise_name(raw)
    registered = split_name_aliases(raw)
    registered_names = [name for role, name in registered if role == "REGISTERED_NAME"]
    registered_raw = registered_names[0] if registered_names else raw
    candidate = normalise_name(registered_raw)
    if extract_legal_form(candidate):
        return False
    tokens = candidate.split()
    if set(tokens) & BUSINESS_NAME_TERMS:
        return False
    # An initial plus surname is the only safe general two-token rule. Merely
    # being two alphabetic words is not person evidence (for example,
    # VOLKSWAGEN UK and Oakwood Autoexchange).
    if re.fullmatch(r"[A-Z]\s+[A-Z][A-Z' -]+", candidate):
        return True
    # Explicit T/A records provide positive sole-trader evidence. Limit the
    # registered segment to a plausible personal-name structure.
    if TRADING_AS_RE.search(raw) and 2 <= len(tokens) <= 4:
        return all(re.fullmatch(r"[A-Z][A-Z' -]+", token) for token in tokens)
    return False


def classify_entity(value: Any, postcode: Any = "") -> str:
    """Routing feature only; it never prevents a Companies House search."""
    x = normalise_name(value)
    compact_pc = normalise_postcode(postcode)
    if any(re.search(pattern, x) for pattern in INTERNATIONAL_PATTERNS):
        return "INTERNATIONAL_ENTITY_LIKELY"
    if compact_pc.isdigit() and compact_pc:
        return "INTERNATIONAL_ENTITY_LIKELY"
    # An explicit legal form is authoritative routing evidence. Public-sector
    # characteristics can still be retained in QA, but must not hide a valid
    # Companies House route.
    if extract_legal_form(x):
        return "COMPANY_LIKELY"
    if any(re.search(pattern, x) for pattern in PUBLIC_BODY_PATTERNS):
        return "PUBLIC_BODY_LIKELY"
    if any(re.search(pattern, x) for pattern in EDUCATION_PATTERNS):
        return "EDUCATION_LIKELY"
    if any(re.search(pattern, x) for pattern in CHARITY_PATTERNS):
        return "CHARITY_LIKELY"
    if _looks_like_person_or_sole_trader(value):
        return "SOLE_TRADER_OR_PERSON_LIKELY"
    return "UNCERTAIN"


def first_significant_token(value: Any) -> str:
    tokens = loose_name(value).split()
    for token in tokens:
        if token not in BLOCK_STOPWORDS and len(token) >= 2:
            return token
    return tokens[0] if tokens else ""


def prefix_key(value: Any, length: int = 6) -> str:
    return loose_name(value).replace(" ", "")[:length]


def length_bucket(value: Any, width: int = 5) -> int:
    return (len(loose_name(value)) // width) * width


def normalise_company_number(value: Any) -> Any:
    x = _text(value).upper()
    if not x:
        return pd.NA
    x = re.sub(r"\.0$", "", x)
    return x.zfill(8)


# %% [markdown]
# ## 3. Similarity features

# %%
def jaro_winkler_similarity(a: Any, b: Any) -> float:
    a, b = _text(a), _text(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if JaroWinkler is not None:
        return float(JaroWinkler.normalized_similarity(a, b))
    match_distance = max(len(a), len(b)) // 2 - 1
    a_matches = [False] * len(a)
    b_matches = [False] * len(b)
    matches = 0
    for i, char in enumerate(a):
        start, end = max(0, i - match_distance), min(i + match_distance + 1, len(b))
        for j in range(start, end):
            if not b_matches[j] and char == b[j]:
                a_matches[i] = True
                b_matches[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    a_chars = [char for char, matched in zip(a, a_matches) if matched]
    b_chars = [char for char, matched in zip(b, b_matches) if matched]
    transpositions = sum(x != y for x, y in zip(a_chars, b_chars)) / 2
    jaro = (
        matches / len(a) + matches / len(b) + (matches - transpositions) / matches
    ) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def levenshtein_similarity(a: Any, b: Any) -> float:
    a, b = _text(a), _text(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if Levenshtein is not None:
        return float(Levenshtein.normalized_similarity(a, b))
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return 1 - previous[-1] / max(len(a), len(b))


def token_sort_similarity(a: Any, b: Any) -> float:
    a_sorted = " ".join(sorted(_text(a).split()))
    b_sorted = " ".join(sorted(_text(b).split()))
    if fuzz is not None:
        return fuzz.ratio(a_sorted, b_sorted) / 100.0
    return SequenceMatcher(None, a_sorted, b_sorted).ratio()


def token_set_similarity(a: Any, b: Any) -> float:
    a_tokens, b_tokens = set(_text(a).split()), set(_text(b).split())
    common = " ".join(sorted(a_tokens & b_tokens))
    a_joined = " ".join(sorted(a_tokens))
    b_joined = " ".join(sorted(b_tokens))
    if fuzz is not None:
        return fuzz.token_set_ratio(a_joined, b_joined) / 100.0
    if not a_joined and not b_joined:
        return 1.0
    return max(
        SequenceMatcher(None, a_joined, b_joined).ratio(),
        SequenceMatcher(None, common, a_joined).ratio() if common else 0.0,
        SequenceMatcher(None, common, b_joined).ratio() if common else 0.0,
    )


def token_jaccard(a: Any, b: Any) -> float:
    ta, tb = set(_text(a).split()), set(_text(b).split())
    if not ta and not tb:
        return 1.0
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def prefix_containment_similarity(a: Any, b: Any) -> float:
    """Rewards a plausible source-truncated prefix without making it decisive."""
    a = loose_name(a)
    b = loose_name(b)
    if not a or not b:
        return 0.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 8 and longer.startswith(shorter):
        return 1.0
    if fuzz is not None:
        return fuzz.ratio(shorter, longer[: len(shorter)]) / 100.0
    return SequenceMatcher(None, shorter, longer[: len(shorter)]).ratio()


def legal_form_comparison(a: Any, b: Any) -> int:
    a, b = _text(a), _text(b)
    if not a or not b:
        return -1
    return int(a == b)


def score_name_pair(alias: pd.Series, company: pd.Series) -> dict[str, Any]:
    a_strict, b_strict = alias["Alias_Strict"], company["CH_Strict"]
    a_loose, b_loose = alias["Alias_Loose"], company["CH_Loose"]

    jw = jaro_winkler_similarity(a_loose, b_loose)
    lev = levenshtein_similarity(a_loose, b_loose)
    tok_sort = token_sort_similarity(a_loose, b_loose)
    tok_set = token_set_similarity(a_loose, b_loose)
    jac = token_jaccard(a_loose, b_loose)
    prefix = prefix_containment_similarity(a_loose, b_loose)
    controlled_exact = int(
        bool(alias["Alias_Controlled"])
        and alias["Alias_Controlled"] == company["CH_Controlled"]
    )
    compact_exact = int(
        len(alias["Alias_Compact"]) >= 8
        and alias["Alias_Compact"] == company["CH_Compact"]
    )
    alias_distinctive = set(alias["Alias_Distinctive_Tokens"])
    company_distinctive = set(company["CH_Distinctive_Tokens"])
    distinctive_overlap = len(alias_distinctive & company_distinctive)

    composite = (
        0.35 * jw
        + 0.20 * lev
        + 0.20 * tok_sort
        + 0.10 * tok_set
        + 0.10 * prefix
        + 0.05 * jac
    )

    return {
        "Best_Alias_Role": alias["Alias_Role"],
        "Best_Alias": alias["Alias_Raw"],
        "Best_Alias_Legal_Form": alias["Alias_Legal_Form"],
        "Strict_Name_Exact": int(bool(a_strict) and a_strict == b_strict),
        "Loose_Name_Exact": int(bool(a_loose) and a_loose == b_loose),
        "Controlled_Name_Exact": controlled_exact,
        "Compact_Name_Exact": compact_exact,
        "Distinctive_Token_Overlap": distinctive_overlap,
        "Recipient_Distinctive_Token_Count": len(alias_distinctive),
        "Name_Jaro_Winkler": jw,
        "Name_Levenshtein": lev,
        "Name_Token_Sort": tok_sort,
        "Name_Token_Set": tok_set,
        "Token_Jaccard": jac,
        "Prefix_Containment": prefix,
        "Name_Composite": composite,
    }


# %% [markdown]
# ## 4. Prepare the full input and unique entity/alias tables

# %%
def read_csv_flexible(path: Path, **kwargs: Any) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin1"):
        try:
            return pd.read_csv(path, encoding=encoding, **kwargs)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise RuntimeError(f"Could not decode {path}") from last_error


def build_input_qa(
    full: pd.DataFrame,
    base: pd.DataFrame,
    config: PipelineConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, bool]:
    """Validate row integrity before an expensive Companies House scan."""
    row_qa = base.copy()
    row_qa["Input_PostCode"] = row_qa[config.grant_postcode_col].map(normalise_postcode)
    row_qa["Input_PostCode_Valid"] = row_qa["Input_PostCode"].map(is_valid_uk_postcode)
    row_qa["Input_Name_Missing"] = (
        row_qa[config.grant_name_col].isna()
        | row_qa[config.grant_name_col].astype("string").str.strip().eq("")
    )
    row_qa["Input_PostCode_Missing"] = row_qa["Input_PostCode"].eq("")

    metrics: list[tuple[str, Any, str]] = [
        ("Input rows", len(full), "INFO"),
        ("Missing recipient names", int(row_qa["Input_Name_Missing"].sum()), "ERROR"),
        ("Missing postcodes", int(row_qa["Input_PostCode_Missing"].sum()), "WARNING"),
        ("Invalid or non-UK postcodes", int((~row_qa["Input_PostCode_Valid"]).sum()), "WARNING"),
    ]

    id_candidates = [column for column in full.columns if str(column).strip().upper() == "ID"]
    duplicate_ids = int(full[id_candidates[0]].duplicated().sum()) if id_candidates else 0
    metrics.append(("Duplicate stable IDs", duplicate_ids, "ERROR" if duplicate_ids else "INFO"))

    comparison = pd.DataFrame()
    alignment_error = False
    reference_file = config.reference_grants_file
    if reference_file is not None and reference_file.exists():
        reference = read_csv_flexible(reference_file, low_memory=False)
        required = {config.grant_name_col, config.grant_postcode_col}
        if required.issubset(reference.columns):
            reference = reference[[config.grant_name_col, config.grant_postcode_col]].copy()
            reference["Reference_PostCode"] = reference[config.grant_postcode_col].map(normalise_postcode)
            reference[config.grant_name_col] = reference[config.grant_name_col].astype("string").str.strip()
            unique_reference = (
                reference.dropna(subset=[config.grant_name_col])
                .drop_duplicates(config.grant_name_col, keep=False)
                .set_index(config.grant_name_col)["Reference_PostCode"]
            )
            comparison = row_qa[
                ["_Input_Row_ID", config.grant_name_col, "Input_PostCode"]
            ].copy()
            comparison["Reference_PostCode"] = comparison[config.grant_name_col].map(unique_reference)
            comparison["Next_Recipient_Name"] = comparison[config.grant_name_col].shift(-1)
            comparison["Next_Name_Reference_PostCode"] = comparison["Next_Recipient_Name"].map(unique_reference)
            comparison["Matches_Own_Reference"] = (
                comparison["Reference_PostCode"].notna()
                & comparison["Input_PostCode"].eq(comparison["Reference_PostCode"])
            )
            comparison["Matches_Next_Row_Reference"] = (
                comparison["Next_Name_Reference_PostCode"].notna()
                & comparison["Input_PostCode"].eq(comparison["Next_Name_Reference_PostCode"])
            )
            overlap = int(comparison["Reference_PostCode"].notna().sum())
            unchanged = int(comparison["Matches_Own_Reference"].sum())
            shift_evidence = int(comparison["Matches_Next_Row_Reference"].sum())
            eligible_shift_rows = int(comparison["Next_Name_Reference_PostCode"].notna().sum())
            shift_rate = shift_evidence / eligible_shift_rows if eligible_shift_rows else 0.0
            alignment_error = shift_evidence >= 3 and shift_rate >= 0.60
            metrics.extend(
                [
                    ("Names overlapping reference file", overlap, "INFO"),
                    ("Overlapping names with unchanged postcode", unchanged, "INFO"),
                    ("Rows matching next recipient's reference postcode", shift_evidence, "CRITICAL" if alignment_error else "INFO"),
                    ("Detected one-row postcode shift", alignment_error, "CRITICAL" if alignment_error else "INFO"),
                ]
            )
    else:
        metrics.append(("Reference-file alignment check", "SKIPPED", "WARNING"))

    qa_summary = pd.DataFrame(metrics, columns=["Check", "Value", "Severity"])
    return row_qa, qa_summary, comparison, alignment_error


def prepare_input(
    config: PipelineConfig,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    bool,
]:
    full = read_csv_flexible(config.grants_file, low_memory=False)
    required = {config.grant_name_col, config.grant_postcode_col}
    missing = required - set(full.columns)
    if missing:
        raise ValueError(f"Missing grant columns: {sorted(missing)}")

    full = full.copy()
    full["_Input_Row_ID"] = np.arange(len(full), dtype=np.int64)

    base = full[
        ["_Input_Row_ID", config.grant_name_col, config.grant_postcode_col]
    ].copy()
    base[config.grant_name_col] = base[config.grant_name_col].astype("string").str.strip()
    base[config.grant_postcode_col] = (
        base[config.grant_postcode_col].astype("string").str.strip()
    )
    row_qa, input_qa_summary, reference_comparison, alignment_error = build_input_qa(
        full, base, config
    )
    valid = base[config.grant_name_col].notna() & base[config.grant_name_col].ne("")

    entities = (
        base.loc[valid, [config.grant_name_col, config.grant_postcode_col]]
        .drop_duplicates()
        .reset_index(drop=True)
    )
    entities["Entity_ID"] = [f"E{i:07d}" for i in range(1, len(entities) + 1)]
    entities["Recipient_Name"] = entities[config.grant_name_col].astype("string")
    entities["Recipient_PostCode_Raw"] = entities[config.grant_postcode_col].astype("string")
    entities["Recipient_Strict"] = entities["Recipient_Name"].map(normalise_name)
    entities["Recipient_Loose"] = entities["Recipient_Name"].map(loose_name)
    entities["Recipient_Legal_Form"] = entities["Recipient_Name"].map(extract_legal_form)
    entities["Recipient_PostCode"] = entities["Recipient_PostCode_Raw"].map(normalise_postcode)
    entities["Recipient_PostCode_Sector"] = entities["Recipient_PostCode"].map(postcode_sector)
    entities["Input_PostCode_Valid"] = entities["Recipient_PostCode"].map(is_valid_uk_postcode)
    entities["Entity_Type"] = [
        classify_entity(name, postcode)
        for name, postcode in zip(
            entities["Recipient_Name"], entities["Recipient_PostCode_Raw"]
        )
    ]
    entities["Source_Name_Possibly_Truncated"] = (
        entities["Recipient_Name"].str.len().fillna(0)
        >= config.suspected_source_name_limit
    )

    row_entity_map = base.merge(
        entities[
            [config.grant_name_col, config.grant_postcode_col, "Entity_ID"]
        ],
        on=[config.grant_name_col, config.grant_postcode_col],
        how="left",
        validate="many_to_one",
    )[["_Input_Row_ID", "Entity_ID"]]

    alias_records: list[dict[str, Any]] = []
    for entity in entities.itertuples(index=False):
        entity_id = getattr(entity, "Entity_ID")
        recipient_name = getattr(entity, "Recipient_Name")
        for alias_no, (role, alias_raw) in enumerate(split_name_aliases(recipient_name), 1):
            alias_records.append(
                {
                    "Entity_ID": entity_id,
                    "Alias_ID": f"{entity_id}_A{alias_no}",
                    "Alias_Role": role,
                    "Alias_Raw": alias_raw,
                    "Alias_Strict": normalise_name(alias_raw),
                    "Alias_Loose": loose_name(alias_raw),
                    "Alias_Legal_Form": extract_legal_form(alias_raw),
                    "Alias_Controlled": controlled_name(alias_raw),
                    "Alias_Compact": compact_name(alias_raw),
                    "Alias_Distinctive_Tokens": tuple(sorted(distinctive_tokens(alias_raw))),
                    "Block_Token": first_significant_token(alias_raw),
                    "Prefix_Key": prefix_key(alias_raw),
                    "Length_Bucket": length_bucket(alias_raw),
                }
            )
    aliases = pd.DataFrame(alias_records)

    LOGGER.info("Input rows: %s", f"{len(full):,}")
    LOGGER.info("Unique name+postcode entities: %s", f"{len(entities):,}")
    LOGGER.info("Generated name aliases: %s", f"{len(aliases):,}")
    return (
        full,
        row_entity_map,
        entities,
        aliases,
        input_qa_summary,
        reference_comparison,
        alignment_error,
    )


# %% [markdown]
# ## 5. Resolve the Companies House schema and stream candidate rows

# %%
CH_CANONICAL_FIELDS = {
    "CompanyName": "CompanyName",
    "CompanyNumber": "CompanyNumber",
    "AddressLine1": "RegAddress.AddressLine1",
    "PostTown": "RegAddress.PostTown",
    "PostCode": "RegAddress.PostCode",
    "CompanyStatus": "CompanyStatus",
}

CH_OPTIONAL_FIELDS = {
    "AddressLine2": "RegAddress.AddressLine2",
    "County": "RegAddress.County",
    "Country": "RegAddress.Country",
    "CompanyCategory": "CompanyCategory",
    "CountryOfOrigin": "CountryOfOrigin",
    "SIC_1": "SICCode.SicText_1",
    "SIC_2": "SICCode.SicText_2",
    "SIC_3": "SICCode.SicText_3",
    "SIC_4": "SICCode.SicText_4",
}


def resolve_ch_columns(
    path: Path,
) -> tuple[dict[str, str], list[dict[str, str | None]]]:
    header = pd.read_csv(path, nrows=0, encoding="latin1")
    stripped = {str(column).strip().lower(): column for column in header.columns}
    resolved: dict[str, str] = {}
    for output_name, expected in CH_CANONICAL_FIELDS.items():
        actual = stripped.get(expected.strip().lower())
        if actual is None:
            raise ValueError(
                f"Companies House column {expected!r} was not found. "
                f"Available columns include: {list(header.columns[:20])}"
            )
        resolved[output_name] = actual
    for output_name, expected in CH_OPTIONAL_FIELDS.items():
        actual = stripped.get(expected.strip().lower())
        if actual is not None:
            resolved[output_name] = actual

    previous_columns: list[dict[str, str | None]] = []
    for previous_no in range(1, 11):
        name_expected = f"PreviousName_{previous_no}.CompanyName"
        date_expected = f"PreviousName_{previous_no}.CONDATE"
        name_actual = stripped.get(name_expected.lower())
        if name_actual is None:
            continue
        previous_columns.append(
            {
                "number": str(previous_no),
                "name_actual": name_actual,
                "date_actual": stripped.get(date_expected.lower()),
                "name_output": f"PreviousName_{previous_no}",
                "date_output": f"PreviousName_{previous_no}_Date",
            }
        )
    return resolved, previous_columns


def _file_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _blocking_signature(
    aliases: pd.DataFrame,
    entities: pd.DataFrame,
    config: PipelineConfig,
) -> dict[str, Any]:
    keys = aliases[
        ["Alias_Strict", "Alias_Loose", "Block_Token", "Prefix_Key", "Length_Bucket"]
    ].astype("string")
    key_lines = ["|".join(row) for row in keys.fillna("").to_numpy().tolist()]
    postcodes = entities["Recipient_PostCode"].fillna("").astype(str).tolist()
    sectors = entities["Recipient_PostCode_Sector"].fillna("").astype(str).tolist()
    digest = hashlib.sha256(
        "\n".join(sorted(key_lines + postcodes + sectors)).encode("utf-8")
    ).hexdigest()
    return {
        "version": 3,
        "companies_house": _file_fingerprint(config.companies_house_file),
        "target_hash": digest,
    }


def _cache_is_valid(config: PipelineConfig, expected: dict[str, Any]) -> bool:
    if config.force_rescan or not config.reuse_candidate_cache:
        return False
    if not config.candidate_cache_file.exists() or not config.candidate_cache_meta_file.exists():
        return False
    try:
        actual = json.loads(config.candidate_cache_meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        actual.get("version") == expected.get("version")
        and actual.get("target_hash") == expected.get("target_hash")
        and actual.get("companies_house") == expected.get("companies_house")
    )


def scan_companies_house(
    aliases: pd.DataFrame,
    entities: pd.DataFrame,
    config: PipelineConfig,
) -> tuple[pd.DataFrame, int]:
    signature = _blocking_signature(aliases, entities, config)
    if _cache_is_valid(config, signature):
        LOGGER.info("Loading valid Companies House candidate cache")
        cached = pd.read_pickle(config.candidate_cache_file)
        cache_meta = json.loads(
            config.candidate_cache_meta_file.read_text(encoding="utf-8")
        )
        return cached, int(cache_meta.get("rows_scanned", 0))

    # V2.3 does not change Companies House scan blocking. Reuse a V2.2 cache
    # only after applying the same source-file and recipient-signature checks.
    if (
        config.reuse_candidate_cache
        and config.reuse_v22_ch_cache
        and not config.force_rescan
        and config.v22_candidate_cache_file.exists()
        and config.v22_candidate_cache_meta_file.exists()
    ):
        try:
            legacy_meta = json.loads(
                config.v22_candidate_cache_meta_file.read_text(encoding="utf-8")
            )
            legacy_valid = (
                legacy_meta.get("version") == signature.get("version")
                and legacy_meta.get("target_hash") == signature.get("target_hash")
                and legacy_meta.get("companies_house") == signature.get("companies_house")
            )
            if legacy_valid:
                LOGGER.info("Promoting valid V2.2 Companies House candidate cache")
                cached = pd.read_pickle(config.v22_candidate_cache_file)
                cached.to_pickle(config.candidate_cache_file)
                config.candidate_cache_meta_file.write_text(
                    json.dumps(legacy_meta, indent=2), encoding="utf-8"
                )
                return cached, int(legacy_meta.get("rows_scanned", 0))
        except (OSError, json.JSONDecodeError, ValueError):
            LOGGER.warning("Ignoring unreadable V2.2 Companies House cache")

    resolved, previous_columns = resolve_ch_columns(config.companies_house_file)
    previous_actual_columns = [
        spec[key]
        for spec in previous_columns
        for key in ("name_actual", "date_actual")
        if spec.get(key)
    ]
    usecols = list(dict.fromkeys(list(resolved.values()) + previous_actual_columns))
    rename_map = {actual: canonical for canonical, actual in resolved.items()}
    for spec in previous_columns:
        rename_map[spec["name_actual"]] = spec["name_output"]
        if spec.get("date_actual"):
            rename_map[spec["date_actual"]] = spec["date_output"]

    strict_set = set(aliases["Alias_Strict"].dropna()) - {""}
    loose_set = set(aliases["Alias_Loose"].dropna()) - {""}
    postcode_set = set(entities["Recipient_PostCode"].dropna()) - {""}
    token_bucket_pairs: set[tuple[str, int]] = set()
    prefix_bucket_pairs: set[tuple[str, int]] = set()
    for alias in aliases.itertuples(index=False):
        bucket = int(alias.Length_Bucket)
        for delta in (-10, -5, 0, 5, 10):
            token_bucket_pairs.add((alias.Block_Token, bucket + delta))
            prefix_bucket_pairs.add((alias.Prefix_Key, bucket + delta))

    alias_blocks = aliases.merge(
        entities[["Entity_ID", "Recipient_PostCode_Sector"]],
        on="Entity_ID",
        how="left",
        validate="many_to_one",
    )
    sector_token_pairs = {
        (row.Recipient_PostCode_Sector, row.Block_Token)
        for row in alias_blocks.itertuples(index=False)
        if row.Recipient_PostCode_Sector and row.Block_Token
    }
    sector_prefix_pairs = {
        (row.Recipient_PostCode_Sector, row.Prefix_Key)
        for row in alias_blocks.itertuples(index=False)
        if row.Recipient_PostCode_Sector and row.Prefix_Key
    }

    candidate_chunks: list[pd.DataFrame] = []
    rows_scanned = 0
    rows_retained = 0
    started = time.perf_counter()

    reader = pd.read_csv(
        config.companies_house_file,
        usecols=usecols,
        dtype="string",
        encoding="latin1",
        chunksize=config.chunk_size,
        low_memory=False,
    )

    for chunk_no, chunk in enumerate(reader, 1):
        rows_scanned += len(chunk)
        chunk = chunk.rename(columns=rename_map)
        chunk = chunk[chunk["CompanyName"].notna()].copy()
        chunk["CompanyNumber"] = chunk["CompanyNumber"].map(normalise_company_number)
        chunk["CH_Strict"] = chunk["CompanyName"].map(normalise_name)
        chunk["CH_Loose"] = chunk["CompanyName"].map(loose_name)
        chunk["CH_Legal_Form"] = chunk["CompanyName"].map(extract_legal_form)
        chunk["CH_PostCode"] = chunk["PostCode"].map(normalise_postcode)
        chunk["CH_PostCode_Sector"] = chunk["CH_PostCode"].map(postcode_sector)
        chunk["Block_Token"] = chunk["CompanyName"].map(first_significant_token)
        chunk["Prefix_Key"] = chunk["CompanyName"].map(prefix_key)
        chunk["Length_Bucket"] = chunk["CompanyName"].map(length_bucket)

        exact_mask = chunk["CH_Strict"].isin(strict_set) | chunk["CH_Loose"].isin(loose_set)
        postcode_mask = chunk["CH_PostCode"].isin(postcode_set)
        token_mask = pd.Series(
            [
                (token, int(bucket)) in token_bucket_pairs
                for token, bucket in zip(chunk["Block_Token"], chunk["Length_Bucket"])
            ],
            index=chunk.index,
            dtype=bool,
        )
        prefix_mask = pd.Series(
            [
                (prefix, int(bucket)) in prefix_bucket_pairs
                for prefix, bucket in zip(chunk["Prefix_Key"], chunk["Length_Bucket"])
            ],
            index=chunk.index,
            dtype=bool,
        )
        sector_token_mask = pd.Series(
            [
                (sector, token) in sector_token_pairs
                for sector, token in zip(
                    chunk["CH_PostCode_Sector"], chunk["Block_Token"]
                )
            ],
            index=chunk.index,
            dtype=bool,
        )
        sector_prefix_mask = pd.Series(
            [
                (sector, prefix) in sector_prefix_pairs
                for sector, prefix in zip(
                    chunk["CH_PostCode_Sector"], chunk["Prefix_Key"]
                )
            ],
            index=chunk.index,
            dtype=bool,
        )
        previous_name_mask = pd.Series(False, index=chunk.index, dtype=bool)
        for spec in previous_columns:
            previous_name_column = spec["name_output"]
            if previous_name_column not in chunk.columns:
                continue
            valid_previous = chunk[previous_name_column].notna() & chunk[previous_name_column].ne("")
            if not valid_previous.any():
                continue
            previous_raw = chunk.loc[valid_previous, previous_name_column]
            previous_strict = previous_raw.map(normalise_name)
            previous_loose = previous_raw.map(loose_name)
            previous_token = previous_raw.map(first_significant_token)
            previous_prefix = previous_raw.map(prefix_key)
            previous_bucket = previous_raw.map(length_bucket)
            previous_sector = chunk.loc[valid_previous, "CH_PostCode_Sector"]
            previous_hit = (
                previous_strict.isin(strict_set)
                | previous_loose.isin(loose_set)
                | pd.Series(
                    [
                        (token, int(bucket)) in token_bucket_pairs
                        for token, bucket in zip(previous_token, previous_bucket)
                    ],
                    index=previous_raw.index,
                    dtype=bool,
                )
                | pd.Series(
                    [
                        (prefix, int(bucket)) in prefix_bucket_pairs
                        for prefix, bucket in zip(previous_prefix, previous_bucket)
                    ],
                    index=previous_raw.index,
                    dtype=bool,
                )
                | pd.Series(
                    [
                        (sector, token) in sector_token_pairs
                        for sector, token in zip(previous_sector, previous_token)
                    ],
                    index=previous_raw.index,
                    dtype=bool,
                )
                | pd.Series(
                    [
                        (sector, prefix) in sector_prefix_pairs
                        for sector, prefix in zip(previous_sector, previous_prefix)
                    ],
                    index=previous_raw.index,
                    dtype=bool,
                )
            )
            previous_name_mask.loc[valid_previous] |= previous_hit
        kept = chunk.loc[
            exact_mask
            | postcode_mask
            | token_mask
            | prefix_mask
            | sector_token_mask
            | sector_prefix_mask
            | previous_name_mask
        ]
        if not kept.empty:
            candidate_chunks.append(kept.copy())
            rows_retained += len(kept)

        if chunk_no % 5 == 0 or len(chunk) < config.chunk_size:
            LOGGER.info(
                "CH chunk %s | scanned %s | retained %s | %.1f min",
                chunk_no,
                f"{rows_scanned:,}",
                f"{rows_retained:,}",
                (time.perf_counter() - started) / 60,
            )

    if candidate_chunks:
        candidates = pd.concat(candidate_chunks, ignore_index=True)
        candidates = candidates.drop_duplicates("CompanyNumber", keep="first").reset_index(drop=True)
    else:
        candidates = pd.DataFrame(
            columns=list(CH_CANONICAL_FIELDS) + [
                "CH_Strict", "CH_Loose", "CH_Legal_Form", "CH_PostCode",
                "CH_PostCode_Sector", "Block_Token", "Prefix_Key", "Length_Bucket",
            ]
        )

    signature["rows_scanned"] = rows_scanned
    candidates.to_pickle(config.candidate_cache_file)
    config.candidate_cache_meta_file.write_text(
        json.dumps(signature, indent=2), encoding="utf-8"
    )

    LOGGER.info("Finished Companies House scan: %s rows", f"{rows_scanned:,}")
    LOGGER.info("Unique CH candidates retained: %s", f"{len(candidates):,}")
    return candidates, rows_scanned


# %% [markdown]
# ## 6. Generate and score entity–company candidate pairs

# %%
def build_index(frame: pd.DataFrame, column: str) -> dict[str, list[int]]:
    index: dict[str, list[int]] = defaultdict(list)
    for row_index, value in frame[column].items():
        key = _text(value)
        if key:
            index[key].append(row_index)
    return index


def build_multi_index(
    frame: pd.DataFrame,
    columns: list[str],
) -> dict[tuple[Any, ...], list[int]]:
    index: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for row_index, values in frame[columns].iterrows():
        key = tuple(values[column] for column in columns)
        if all(_text(value) for value in key):
            index[key].append(row_index)
    return index


def build_ch_name_variants(ch_candidates: pd.DataFrame) -> pd.DataFrame:
    """Create one searchable row per current or historical Companies House name."""
    variants: list[pd.DataFrame] = []

    def add_variant(
        source: pd.Series,
        source_label: str,
        date_values: pd.Series | None = None,
    ) -> None:
        valid = source.notna() & source.astype("string").str.strip().ne("")
        if not valid.any():
            return
        frame = pd.DataFrame(
            {
                "_CH_Row_ID": ch_candidates.index[valid],
                "CH_MatchedName": source.loc[valid].astype("string").str.strip().to_numpy(),
                "CH_NameSource": source_label,
                "CH_PreviousNameDate": (
                    date_values.loc[valid].to_numpy()
                    if date_values is not None
                    else pd.array([pd.NA] * int(valid.sum()), dtype="string")
                ),
                "CH_PostCode": ch_candidates.loc[valid, "CH_PostCode"].to_numpy(),
                "CH_PostCode_Sector": ch_candidates.loc[valid, "CH_PostCode_Sector"].to_numpy(),
            }
        )
        frame["CH_Strict"] = frame["CH_MatchedName"].map(normalise_name)
        frame["CH_Loose"] = frame["CH_MatchedName"].map(loose_name)
        frame["CH_Controlled"] = frame["CH_MatchedName"].map(controlled_name)
        frame["CH_Compact"] = frame["CH_MatchedName"].map(compact_name)
        frame["CH_Distinctive_Tokens"] = frame["CH_MatchedName"].map(
            lambda value: tuple(sorted(distinctive_tokens(value)))
        )
        frame["CH_Legal_Form"] = frame["CH_MatchedName"].map(extract_legal_form)
        frame["Block_Token"] = frame["CH_MatchedName"].map(first_significant_token)
        frame["Prefix_Key"] = frame["CH_MatchedName"].map(prefix_key)
        frame["Length_Bucket"] = frame["CH_MatchedName"].map(length_bucket)
        variants.append(frame)

    add_variant(ch_candidates["CompanyName"], "CURRENT")
    for previous_no in range(1, 11):
        name_column = f"PreviousName_{previous_no}"
        if name_column not in ch_candidates.columns:
            continue
        date_column = f"PreviousName_{previous_no}_Date"
        date_values = (
            ch_candidates[date_column]
            if date_column in ch_candidates.columns
            else None
        )
        add_variant(ch_candidates[name_column], f"PREVIOUS_{previous_no}", date_values)

    if not variants:
        return pd.DataFrame()
    return pd.concat(variants, ignore_index=True)


def _best_alias_and_name_score(
    entity_aliases: pd.DataFrame,
    company_variants: pd.DataFrame,
) -> dict[str, Any]:
    scored: list[dict[str, Any]] = []
    for _, alias in entity_aliases.iterrows():
        for _, variant in company_variants.iterrows():
            result = score_name_pair(alias, variant)
            result.update(
                {
                    "CH_MatchedName": variant["CH_MatchedName"],
                    "CH_NameSource": variant["CH_NameSource"],
                    "CH_PreviousNameDate": variant["CH_PreviousNameDate"],
                    "CH_MatchedName_Legal_Form": variant["CH_Legal_Form"],
                }
            )
            scored.append(result)

    best = max(
        scored,
        key=lambda item: (
            item["Strict_Name_Exact"],
            item["Loose_Name_Exact"],
            item["Controlled_Name_Exact"],
            item["Compact_Name_Exact"],
            item["Distinctive_Token_Overlap"],
            item["Name_Composite"],
            item["Best_Alias_Role"] != "TRADING_NAME",
            item["CH_NameSource"] == "CURRENT",
        ),
    )
    best["Any_Current_Name_Exact"] = int(
        any(
            item["CH_NameSource"] == "CURRENT"
            and (item["Strict_Name_Exact"] or item["Loose_Name_Exact"])
            for item in scored
        )
    )
    best["Any_Alias_Name_Exact"] = int(
        any(
            item["CH_NameSource"] != "CURRENT"
            and (item["Strict_Name_Exact"] or item["Loose_Name_Exact"])
            for item in scored
        )
    )
    best["Any_Controlled_Or_Compact_Exact"] = int(
        any(
            item["Controlled_Name_Exact"] or item["Compact_Name_Exact"]
            for item in scored
        )
    )
    best["Max_Prefix_Containment"] = max(
        item["Prefix_Containment"] for item in scored
    )
    best["Max_Distinctive_Token_Overlap"] = max(
        item["Distinctive_Token_Overlap"] for item in scored
    )
    return best


def generate_candidate_pairs(
    entities: pd.DataFrame,
    aliases: pd.DataFrame,
    ch_candidates: pd.DataFrame,
    config: PipelineConfig,
) -> pd.DataFrame:
    if ch_candidates.empty:
        return pd.DataFrame()

    name_variants = build_ch_name_variants(ch_candidates)
    if name_variants.empty:
        return pd.DataFrame()
    indexes = {
        "STRICT": build_index(name_variants, "CH_Strict"),
        "LOOSE": build_index(name_variants, "CH_Loose"),
        "CONTROLLED": build_index(name_variants, "CH_Controlled"),
        "COMPACT": build_index(name_variants, "CH_Compact"),
        "TOKEN_BUCKET": build_multi_index(
            name_variants, ["Block_Token", "Length_Bucket"]
        ),
        "PREFIX_BUCKET": build_multi_index(
            name_variants, ["Prefix_Key", "Length_Bucket"]
        ),
        "SECTOR_TOKEN": build_multi_index(
            name_variants, ["CH_PostCode_Sector", "Block_Token"]
        ),
        "SECTOR_PREFIX": build_multi_index(
            name_variants, ["CH_PostCode_Sector", "Prefix_Key"]
        ),
    }
    postcode_index = build_index(ch_candidates, "CH_PostCode")
    variant_company_ids = name_variants["_CH_Row_ID"].astype(int).to_dict()
    variants_by_company = {
        int(company_id): frame
        for company_id, frame in name_variants.groupby("_CH_Row_ID", sort=False)
    }

    entity_lookup = entities.set_index("Entity_ID", drop=False)
    pair_records: list[dict[str, Any]] = []

    for entity_id, entity_aliases in aliases.groupby("Entity_ID", sort=False):
        entity = entity_lookup.loc[entity_id]
        candidate_rules: dict[int, set[str]] = defaultdict(set)

        def add(index_name: str, key: Any, rule: str) -> None:
            if not _text(key):
                return
            for variant_index in indexes[index_name].get(_text(key), []):
                candidate_rules[variant_company_ids[variant_index]].add(rule)

        def add_multi(index_name: str, key: tuple[Any, ...], rule: str) -> None:
            if not all(_text(value) for value in key):
                return
            for variant_index in indexes[index_name].get(key, []):
                candidate_rules[variant_company_ids[variant_index]].add(rule)

        for alias in entity_aliases.itertuples(index=False):
            add("STRICT", alias.Alias_Strict, "EXACT_STRICT")
            add("LOOSE", alias.Alias_Loose, "EXACT_LOOSE")
            add("CONTROLLED", alias.Alias_Controlled, "EXACT_CONTROLLED")
            add("COMPACT", alias.Alias_Compact, "EXACT_COMPACT")
            for delta in (-10, -5, 0, 5, 10):
                add_multi(
                    "TOKEN_BUCKET",
                    (alias.Block_Token, int(alias.Length_Bucket) + delta),
                    "BLOCK_TOKEN_LENGTH",
                )
                add_multi(
                    "PREFIX_BUCKET",
                    (alias.Prefix_Key, int(alias.Length_Bucket) + delta),
                    "PREFIX_LENGTH",
                )
            add_multi(
                "SECTOR_TOKEN",
                (entity["Recipient_PostCode_Sector"], alias.Block_Token),
                "SECTOR_TOKEN",
            )
            add_multi(
                "SECTOR_PREFIX",
                (entity["Recipient_PostCode_Sector"], alias.Prefix_Key),
                "SECTOR_PREFIX",
            )
        for ch_index in postcode_index.get(_text(entity["Recipient_PostCode"]), []):
            candidate_rules[int(ch_index)].add("EXACT_POSTCODE")

        local: list[dict[str, Any]] = []
        for ch_index, rules in candidate_rules.items():
            company = ch_candidates.loc[ch_index]
            name_scores = _best_alias_and_name_score(
                entity_aliases, variants_by_company[ch_index]
            )
            postcode_exact = int(
                bool(entity["Recipient_PostCode"])
                and entity["Recipient_PostCode"] == company["CH_PostCode"]
            )
            sector_exact = int(
                bool(entity["Recipient_PostCode_Sector"])
                and entity["Recipient_PostCode_Sector"] == company["CH_PostCode_Sector"]
            )

            if not (
                name_scores["Name_Composite"] >= config.min_cheap_name_score
                or postcode_exact
                or name_scores["Strict_Name_Exact"]
                or name_scores["Loose_Name_Exact"]
                or name_scores["Controlled_Name_Exact"]
                or name_scores["Compact_Name_Exact"]
            ):
                continue

            local.append(
                {
                    "Entity_ID": entity_id,
                    "Recipient_Name": entity["Recipient_Name"],
                    "Recipient_PostCode": entity["Recipient_PostCode"],
                    "Recipient_PostCode_Sector": entity["Recipient_PostCode_Sector"],
                    "Recipient_Legal_Form": entity["Recipient_Legal_Form"],
                    "Entity_Type": entity["Entity_Type"],
                    "Input_PostCode_Valid": entity["Input_PostCode_Valid"],
                    "Source_Name_Possibly_Truncated": entity["Source_Name_Possibly_Truncated"],
                    "CH_CompanyName": company["CompanyName"],
                    "CH_CompanyNumber": company["CompanyNumber"],
                    "CH_AddressLine1": company["AddressLine1"],
                    "CH_PostTown": company["PostTown"],
                    "CH_PostCode": company["PostCode"],
                    "CH_CompanyStatus": company["CompanyStatus"],
                    "CH_CompanyCategory": company.get("CompanyCategory", pd.NA),
                    "CH_CountryOfOrigin": company.get("CountryOfOrigin", pd.NA),
                    "Blocking_Rules": "|".join(sorted(rules)),
                    **name_scores,
                    "PostCode_Exact": postcode_exact,
                    "PostCode_Sector_Exact": sector_exact,
                    "Truncated_Prefix_PostCode": int(
                        bool(entity["Source_Name_Possibly_Truncated"])
                        and name_scores["Prefix_Containment"] >= 0.99
                        and postcode_exact == 1
                    ),
                    "Legal_Form_Comparison": legal_form_comparison(
                        name_scores["Best_Alias_Legal_Form"],
                        name_scores["CH_MatchedName_Legal_Form"],
                    ),
                    "CH_Status_Active": int(
                        _text(company["CompanyStatus"]).upper() == "ACTIVE"
                    ),
                }
            )

        if local:
            local_frame = pd.DataFrame(local).sort_values(
                [
                    "Strict_Name_Exact", "Loose_Name_Exact",
                    "Controlled_Name_Exact", "Compact_Name_Exact",
                    "PostCode_Exact", "Distinctive_Token_Overlap",
                    "Name_Composite", "CH_Status_Active",
                ],
                ascending=[False, False, False, False, False, False, False, False],
            )
            pre_cap_count = len(local_frame)
            local_frame["Pre_Cap_Count"] = pre_cap_count
            local_frame["Candidate_Cap"] = config.max_candidates_per_entity
            local_frame["Cap_Saturated"] = int(
                pre_cap_count > config.max_candidates_per_entity
            )
            local_frame["Candidate_Cap_Type"] = "COMPANIES_HOUSE"
            pair_records.extend(
                local_frame.head(config.max_candidates_per_entity).to_dict("records")
            )

    pairs = pd.DataFrame(pair_records)
    LOGGER.info("Candidate pairs generated: %s", f"{len(pairs):,}")
    LOGGER.info(
        "Entities with at least one candidate: %s",
        f"{pairs['Entity_ID'].nunique() if not pairs.empty else 0:,}",
    )
    return pairs


# %% [markdown]
# ## 7. Load and search the three UK charity registers

# %%
CHARITY_CANONICAL_COLUMNS = [
    "Registry_Record_Key", "External_Registry", "External_Jurisdiction",
    "External_Registry_ID", "External_Registry_Display_ID", "External_Linked_ID",
    "External_Legal_Name", "External_Known_As", "External_PostCode",
    "External_Address", "External_Status", "External_CompanyNumber",
    "External_Constitutional_Form", "External_Website", "External_Registry_Active",
]


def _clean_registry_identifier(value: Any) -> Any:
    value = re.sub(r"\.0$", "", _text(value))
    return pd.NA if not value or value == "0" else value


def _clean_external_company_number(value: Any) -> Any:
    value = _clean_registry_identifier(value)
    return pd.NA if pd.isna(value) else normalise_company_number(value)


def _join_address_columns(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    available = [column for column in columns if column in frame.columns]
    if not available:
        return pd.Series([""] * len(frame), index=frame.index, dtype="string")
    return frame[available].fillna("").astype("string").apply(
        lambda row: ", ".join(part.strip() for part in row if part.strip()), axis=1
    )


def _load_charity_ew_other_names(path: Path) -> dict[str, str]:
    """Return Charity Commission aliases keyed by organisation number.

    The Commission has used slightly different labels across extract versions,
    so the header is resolved rather than hard-coded. The optional extract is
    never required for the pipeline to run.
    """
    separator = "\t"
    header = pd.read_csv(
        path, compression="zip", sep=separator, dtype="string", nrows=0
    )
    if len(header.columns) == 1:
        separator = ","
        header = pd.read_csv(
            path, compression="zip", sep=separator, dtype="string", nrows=0
        )
    normalized = {
        re.sub(r"[^a-z0-9]+", "_", str(column).strip().lower()).strip("_"): column
        for column in header.columns
    }
    org_column = next(
        (normalized[key] for key in ("organisation_number", "organization_number") if key in normalized),
        None,
    )
    name_column = next(
        (
            normalized[key]
            for key in ("charity_name", "other_name", "charity_other_name", "name")
            if key in normalized
        ),
        None,
    )
    if org_column is None or name_column is None:
        raise ValueError(
            "Could not resolve organisation/name columns in Charity Commission "
            f"other-names extract. Columns: {list(header.columns)}"
        )

    grouped: dict[str, set[str]] = defaultdict(set)
    for chunk in pd.read_csv(
        path,
        compression="zip",
        sep=separator,
        usecols=[org_column, name_column],
        dtype="string",
        chunksize=100_000,
        low_memory=False,
    ):
        for organisation, name in zip(chunk[org_column], chunk[name_column]):
            organisation_key = _text(_clean_registry_identifier(organisation))
            cleaned_name = _text(name).strip()
            if organisation_key and cleaned_name:
                grouped[organisation_key].add(cleaned_name)
    return {
        organisation: "; ".join(sorted(names, key=normalise_name))
        for organisation, names in grouped.items()
    }


def _load_charity_ew(
    path: Path,
    other_names_path: Path | None = None,
) -> pd.DataFrame:
    other_names: dict[str, str] = {}
    if other_names_path is not None and other_names_path.is_file():
        other_names = _load_charity_ew_other_names(other_names_path)
        LOGGER.info(
            "Loaded historical/other names for %s Charity Commission organisations",
            f"{len(other_names):,}",
        )
    usecols = [
        "organisation_number", "registered_charity_number", "linked_charity_number",
        "charity_name", "charity_type", "charity_registration_status",
        "charity_contact_address1", "charity_contact_address2",
        "charity_contact_address3", "charity_contact_address4",
        "charity_contact_address5", "charity_contact_postcode",
        "charity_contact_web", "charity_company_registration_number",
    ]
    output: list[pd.DataFrame] = []
    for chunk in pd.read_csv(
        path, compression="zip", sep="\t", usecols=usecols, dtype="string",
        chunksize=100_000, low_memory=False,
    ):
        registered = chunk["registered_charity_number"].map(_clean_registry_identifier)
        linked = chunk["linked_charity_number"].map(_clean_registry_identifier)
        display_id = registered.astype("string")
        linked_mask = linked.notna()
        display_id.loc[linked_mask] = (
            registered.loc[linked_mask].astype("string")
            + "-" + linked.loc[linked_mask].astype("string")
        )
        status = chunk["charity_registration_status"].fillna("").astype("string")
        canonical = pd.DataFrame(
            {
                "Registry_Record_Key": "CCEW:" + chunk["organisation_number"].astype("string"),
                "External_Registry": "CHARITY_COMMISSION_EW",
                "External_Jurisdiction": "ENGLAND_WALES",
                "External_Registry_ID": registered,
                "External_Registry_Display_ID": display_id,
                "External_Linked_ID": linked,
                "External_Legal_Name": chunk["charity_name"],
                "External_Known_As": chunk["organisation_number"].map(
                    lambda value: other_names.get(_text(_clean_registry_identifier(value)), "")
                ),
                "External_PostCode": chunk["charity_contact_postcode"].map(normalise_postcode),
                "External_Address": _join_address_columns(
                    chunk,
                    [
                        "charity_contact_address1", "charity_contact_address2",
                        "charity_contact_address3", "charity_contact_address4",
                        "charity_contact_address5",
                    ],
                ),
                "External_Status": status,
                "External_CompanyNumber": chunk["charity_company_registration_number"].map(
                    _clean_external_company_number
                ),
                "External_Constitutional_Form": chunk["charity_type"],
                "External_Website": chunk["charity_contact_web"],
                "External_Registry_Active": status.str.upper().eq("REGISTERED").astype(int),
            }
        )
        output.append(canonical)
    return pd.concat(output, ignore_index=True) if output else pd.DataFrame(columns=CHARITY_CANONICAL_COLUMNS)


def _load_charity_scotland(path: Path) -> pd.DataFrame:
    usecols = [
        "Charity Number", "Charity Name", "Known As", "Charity Status", "Postcode",
        "Constitutional Form", "Principal Office/Trustees Address", "Website",
    ]
    frame = pd.read_csv(
        path, compression="zip", usecols=usecols, dtype="string", low_memory=False
    )
    status = frame["Charity Status"].fillna("").astype("string")
    inactive = status.str.upper().str.contains(r"REMOVED|CEASED|DEREGISTERED", regex=True)
    return pd.DataFrame(
        {
            "Registry_Record_Key": "OSCR:" + frame["Charity Number"].astype("string"),
            "External_Registry": "OSCR",
            "External_Jurisdiction": "SCOTLAND",
            "External_Registry_ID": frame["Charity Number"].map(_clean_registry_identifier),
            "External_Registry_Display_ID": frame["Charity Number"].map(_clean_registry_identifier),
            "External_Linked_ID": pd.NA,
            "External_Legal_Name": frame["Charity Name"],
            "External_Known_As": frame["Known As"],
            "External_PostCode": frame["Postcode"].map(normalise_postcode),
            "External_Address": frame["Principal Office/Trustees Address"],
            "External_Status": status,
            "External_CompanyNumber": pd.NA,
            "External_Constitutional_Form": frame["Constitutional Form"],
            "External_Website": frame["Website"],
            "External_Registry_Active": (~inactive).astype(int),
        }
    )


def _load_charity_ni(path: Path) -> pd.DataFrame:
    frame = read_csv_flexible(path, dtype="string", low_memory=False)
    required = {
        "Reg charity number", "Charity name", "Status", "Public address",
        "Company number", "Other name", "Type of governing document", "Website",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Missing CCNI export columns: {sorted(missing)}")
    status = frame["Status"].fillna("").astype("string")
    inactive = status.str.upper().str.contains(r"REMOVED|CEASED|DEREGISTERED", regex=True)
    return pd.DataFrame(
        {
            "Registry_Record_Key": "CCNI:" + frame["Reg charity number"].astype("string"),
            "External_Registry": "CCNI",
            "External_Jurisdiction": "NORTHERN_IRELAND",
            "External_Registry_ID": frame["Reg charity number"].map(_clean_registry_identifier),
            "External_Registry_Display_ID": frame["Reg charity number"].map(_clean_registry_identifier),
            "External_Linked_ID": pd.NA,
            "External_Legal_Name": frame["Charity name"],
            "External_Known_As": frame["Other name"],
            "External_PostCode": frame["Public address"].map(extract_uk_postcode),
            "External_Address": frame["Public address"],
            "External_Status": status,
            "External_CompanyNumber": frame["Company number"].map(
                _clean_external_company_number
            ),
            "External_Constitutional_Form": frame["Type of governing document"],
            "External_Website": frame["Website"],
            "External_Registry_Active": (~inactive).astype(int),
        }
    )


def load_charity_registries(config: PipelineConfig) -> pd.DataFrame:
    if not config.use_charity_registries:
        return pd.DataFrame(columns=CHARITY_CANONICAL_COLUMNS)
    source_paths = [
        config.charity_ew_file,
        config.charity_scotland_file,
        config.charity_ni_file,
    ]
    signature: dict[str, Any] = {
        "version": 2,
        "normalisation": "v2_3_charity_variants",
        "files": {str(path.resolve()): _file_fingerprint(path) for path in source_paths},
    }
    if config.charity_ew_other_names_file.is_file():
        signature["files"][str(config.charity_ew_other_names_file.resolve())] = (
            _file_fingerprint(config.charity_ew_other_names_file)
        )
    elif config.charity_ew_other_names_filename:
        LOGGER.warning(
            "Optional Charity Commission other-names extract not found: %s",
            config.charity_ew_other_names_file,
        )

    if config.reuse_candidate_cache and not config.force_rescan:
        if config.charity_cache_file.exists() and config.charity_cache_meta_file.exists():
            try:
                actual = json.loads(
                    config.charity_cache_meta_file.read_text(encoding="utf-8")
                )
                if actual == signature:
                    LOGGER.info("Loading valid normalized charity-registry cache")
                    return pd.read_pickle(config.charity_cache_file)
            except (OSError, json.JSONDecodeError, ValueError):
                LOGGER.warning("Ignoring unreadable charity-registry cache")

    loaders = [
        (
            config.charity_ew_file,
            lambda path: _load_charity_ew(path, config.charity_ew_other_names_file),
        ),
        (config.charity_scotland_file, _load_charity_scotland),
        (config.charity_ni_file, _load_charity_ni),
    ]
    frames: list[pd.DataFrame] = []
    for path, loader in loaders:
        frame = loader(path)
        LOGGER.info("Loaded %s charity records from %s", f"{len(frame):,}", path.name)
        frames.append(frame)
    registries = pd.concat(frames, ignore_index=True)
    registries = registries.drop_duplicates("Registry_Record_Key").reset_index(drop=True)
    registries["External_PostCode_Sector"] = registries["External_PostCode"].map(postcode_sector)
    cache_temp = config.charity_cache_file.with_suffix(".tmp.pkl")
    meta_temp = config.charity_cache_meta_file.with_suffix(".tmp.json")
    registries.to_pickle(cache_temp)
    meta_temp.write_text(json.dumps(signature, indent=2), encoding="utf-8")
    cache_temp.replace(config.charity_cache_file)
    meta_temp.replace(config.charity_cache_meta_file)
    LOGGER.info("Combined charity registry records: %s", f"{len(registries):,}")
    return registries


def _split_external_aliases(value: Any) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,;|\n]+", _text(value)):
        item = item.strip()
        key = normalise_name(item)
        if key and key not in seen:
            output.append(item)
            seen.add(key)
    return output


def build_charity_name_variants(registries: pd.DataFrame) -> pd.DataFrame:
    base = pd.DataFrame(
        {
            "_External_Row_ID": registries.index,
            "CH_MatchedName": registries["External_Legal_Name"].astype("string").fillna(""),
            "CH_NameSource": "CURRENT",
            "CH_PreviousNameDate": pd.NA,
            "CH_PostCode": registries["External_PostCode"].to_numpy(),
            "CH_PostCode_Sector": registries["External_PostCode_Sector"].to_numpy(),
        }
    )
    base = base[base["CH_MatchedName"].str.strip().ne("")].copy()

    alias_records: list[dict[str, Any]] = []
    known_as = registries["External_Known_As"].fillna("").astype("string")
    for row_index, value in known_as[known_as.str.strip().ne("")].items():
        legal_key = normalise_name(registries.at[row_index, "External_Legal_Name"])
        for alias_no, alias in enumerate(_split_external_aliases(value), 1):
            if normalise_name(alias) == legal_key:
                continue
            alias_records.append(
                {
                    "_External_Row_ID": row_index,
                    "CH_MatchedName": alias,
                    "CH_NameSource": f"ALIAS_{alias_no}",
                    "CH_PreviousNameDate": pd.NA,
                    "CH_PostCode": registries.at[row_index, "External_PostCode"],
                    "CH_PostCode_Sector": registries.at[
                        row_index, "External_PostCode_Sector"
                    ],
                }
            )
    if alias_records:
        base = pd.concat([base, pd.DataFrame(alias_records)], ignore_index=True)

    base["CH_Strict"] = base["CH_MatchedName"].map(normalise_name)
    base["CH_Loose"] = base["CH_MatchedName"].map(loose_name)
    base["CH_Controlled"] = base["CH_MatchedName"].map(controlled_name)
    base["CH_Compact"] = base["CH_MatchedName"].map(compact_name)
    base["CH_Distinctive_Tokens"] = base["CH_MatchedName"].map(
        lambda name: tuple(sorted(distinctive_tokens(name)))
    )
    base["CH_Legal_Form"] = base["CH_MatchedName"].map(extract_legal_form)
    base["Block_Token"] = base["CH_MatchedName"].map(first_significant_token)
    base["Prefix_Key"] = base["CH_MatchedName"].map(prefix_key)
    base["Length_Bucket"] = base["CH_MatchedName"].map(length_bucket)
    return base.reset_index(drop=True)


def generate_charity_candidate_pairs(
    entities: pd.DataFrame,
    aliases: pd.DataFrame,
    registries: pd.DataFrame,
    config: PipelineConfig,
    ranked_ch_pairs: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if registries.empty:
        return pd.DataFrame()
    variants = build_charity_name_variants(registries)
    indexes = {
        "STRICT": build_index(variants, "CH_Strict"),
        "LOOSE": build_index(variants, "CH_Loose"),
        "CONTROLLED": build_index(variants, "CH_Controlled"),
        "COMPACT": build_index(variants, "CH_Compact"),
        "TOKEN_BUCKET": build_multi_index(variants, ["Block_Token", "Length_Bucket"]),
        "PREFIX_BUCKET": build_multi_index(variants, ["Prefix_Key", "Length_Bucket"]),
        "SECTOR_TOKEN": build_multi_index(variants, ["CH_PostCode_Sector", "Block_Token"]),
        "SECTOR_PREFIX": build_multi_index(variants, ["CH_PostCode_Sector", "Prefix_Key"]),
    }
    postcode_index = build_index(registries, "External_PostCode")
    company_number_index = build_index(registries, "External_CompanyNumber")
    variant_registry_ids = variants["_External_Row_ID"].astype(int).to_dict()
    variants_by_record = {
        int(record_id): frame
        for record_id, frame in variants.groupby("_External_Row_ID", sort=False)
    }
    long_prefix_index: dict[str, list[int]] = defaultdict(list)
    for variant_index, charity_loose in variants["CH_Loose"].items():
        charity_loose = _text(charity_loose)
        if len(charity_loose) >= 20:
            long_prefix_index[charity_loose[:20]].append(variant_index)

    ch_by_entity: dict[str, pd.DataFrame] = {}
    if ranked_ch_pairs is not None and not ranked_ch_pairs.empty:
        eligible_ch = ranked_ch_pairs[
            ranked_ch_pairs["Candidate_Rank"].le(
                config.crosswalk_candidate_rank_limit
            )
        ].copy()
        eligible_ch["_Crosswalk_CompanyNumber"] = eligible_ch[
            "CH_CompanyNumber"
        ].map(normalise_company_number)
        ch_by_entity = {
            str(entity_id): frame.sort_values("Candidate_Rank")
            for entity_id, frame in eligible_ch.groupby("Entity_ID", sort=False)
        }
    entity_lookup = entities.set_index("Entity_ID", drop=False)
    pair_records: list[dict[str, Any]] = []

    for entity_id, entity_aliases in aliases.groupby("Entity_ID", sort=False):
        entity = entity_lookup.loc[entity_id]
        candidate_rules: dict[int, set[str]] = defaultdict(set)

        def add(index_name: str, key: Any, rule: str) -> None:
            if not _text(key):
                return
            for variant_index in indexes[index_name].get(_text(key), []):
                candidate_rules[variant_registry_ids[variant_index]].add(rule)

        def add_multi(index_name: str, key: tuple[Any, ...], rule: str) -> None:
            if not all(_text(value) for value in key):
                return
            for variant_index in indexes[index_name].get(key, []):
                candidate_rules[variant_registry_ids[variant_index]].add(rule)

        for alias in entity_aliases.itertuples(index=False):
            add("STRICT", alias.Alias_Strict, "EXACT_STRICT")
            add("LOOSE", alias.Alias_Loose, "EXACT_LOOSE")
            add("CONTROLLED", alias.Alias_Controlled, "EXACT_CONTROLLED")
            add("COMPACT", alias.Alias_Compact, "EXACT_COMPACT")
            for delta in (-10, -5, 0, 5, 10):
                add_multi(
                    "TOKEN_BUCKET",
                    (alias.Block_Token, int(alias.Length_Bucket) + delta),
                    "BLOCK_TOKEN_LENGTH",
                )
            for delta in (-20, -15, -10, -5, 0, 5, 10, 15, 20):
                add_multi(
                    "PREFIX_BUCKET",
                    (alias.Prefix_Key, int(alias.Length_Bucket) + delta),
                    "PREFIX_LENGTH",
                )
            add_multi(
                "SECTOR_TOKEN",
                (entity["Recipient_PostCode_Sector"], alias.Block_Token),
                "SECTOR_TOKEN",
            )
            add_multi(
                "SECTOR_PREFIX",
                (entity["Recipient_PostCode_Sector"], alias.Prefix_Key),
                "SECTOR_PREFIX",
            )
            alias_loose = _text(alias.Alias_Loose)
            if len(alias_loose) >= 20:
                for variant_index in long_prefix_index.get(alias_loose[:20], []):
                    charity_loose = _text(variants.at[variant_index, "CH_Loose"])
                    if charity_loose.startswith(alias_loose):
                        candidate_rules[
                            variant_registry_ids[variant_index]
                        ].add("LONG_PREFIX_CONTAINMENT")

        entity_ch = ch_by_entity.get(str(entity_id), pd.DataFrame())
        crosswalk_evidence: dict[str, pd.Series] = {}
        if not entity_ch.empty:
            for _, ch_row in entity_ch.iterrows():
                company_number = _text(ch_row["_Crosswalk_CompanyNumber"])
                if not company_number:
                    continue
                crosswalk_evidence.setdefault(company_number, ch_row)
                for record_index in company_number_index.get(company_number, []):
                    candidate_rules[int(record_index)].add(
                        "CH_COMPANY_NUMBER_CROSSWALK"
                    )
        for record_index in postcode_index.get(_text(entity["Recipient_PostCode"]), []):
            candidate_rules[int(record_index)].add("EXACT_POSTCODE")

        local: list[dict[str, Any]] = []
        home_jurisdiction = postcode_jurisdiction(entity["Recipient_PostCode"])
        for record_index, rules in candidate_rules.items():
            registry_row = registries.loc[record_index]
            name_scores = _best_alias_and_name_score(
                entity_aliases, variants_by_record[record_index]
            )
            postcode_exact = int(
                bool(entity["Recipient_PostCode"])
                and entity["Recipient_PostCode"] == registry_row["External_PostCode"]
            )
            sector_exact = int(
                bool(entity["Recipient_PostCode_Sector"])
                and entity["Recipient_PostCode_Sector"]
                == registry_row["External_PostCode_Sector"]
            )
            external_company_number = _text(
                normalise_company_number(registry_row["External_CompanyNumber"])
            )
            crosswalk_ch = crosswalk_evidence.get(external_company_number)
            crosswalk_flag = int(crosswalk_ch is not None)
            crosswalk_rank = (
                int(crosswalk_ch["Candidate_Rank"])
                if crosswalk_ch is not None
                else pd.NA
            )
            crosswalk_current_exact = int(
                crosswalk_ch is not None
                and int(crosswalk_ch.get("Current_Name_Exact", 0)) == 1
            )
            crosswalk_previous_exact = int(
                crosswalk_ch is not None
                and int(crosswalk_ch.get("Previous_Name_Exact", 0)) == 1
            )
            crosswalk_truncated_pc = int(
                crosswalk_ch is not None
                and int(crosswalk_ch.get("Truncated_Prefix_PostCode", 0)) == 1
            )
            crosswalk_deterministic_name = int(
                crosswalk_current_exact
                or crosswalk_previous_exact
                or crosswalk_truncated_pc
            )
            if not (
                name_scores["Name_Composite"] >= config.min_cheap_name_score
                or name_scores["Strict_Name_Exact"]
                or name_scores["Loose_Name_Exact"]
                or name_scores["Controlled_Name_Exact"]
                or name_scores["Compact_Name_Exact"]
                or crosswalk_flag
            ):
                continue
            local.append(
                {
                    "Entity_ID": entity_id,
                    "Recipient_Name": entity["Recipient_Name"],
                    "Recipient_PostCode": entity["Recipient_PostCode"],
                    "Recipient_PostCode_Sector": entity["Recipient_PostCode_Sector"],
                    "Entity_Type": entity["Entity_Type"],
                    "Input_PostCode_Valid": entity["Input_PostCode_Valid"],
                    "Recipient_Home_Jurisdiction": home_jurisdiction,
                    "Registry_Preference": int(
                        home_jurisdiction != "UNKNOWN"
                        and home_jurisdiction != registry_row["External_Jurisdiction"]
                    ),
                    "Blocking_Rules": "|".join(sorted(rules)),
                    **{column: registry_row[column] for column in CHARITY_CANONICAL_COLUMNS},
                    **name_scores,
                    "External_MatchedName": name_scores["CH_MatchedName"],
                    "External_NameSource": name_scores["CH_NameSource"],
                    "PostCode_Exact": postcode_exact,
                    "PostCode_Sector_Exact": sector_exact,
                    "External_CompanyNumber_Crosswalk": crosswalk_flag,
                    "Crosswalk_CH_Candidate_Rank": crosswalk_rank,
                    "Crosswalk_CH_Current_Name_Exact": crosswalk_current_exact,
                    "Crosswalk_CH_Previous_Name_Exact": crosswalk_previous_exact,
                    "Crosswalk_CH_Truncated_Prefix_PostCode": crosswalk_truncated_pc,
                    "Crosswalk_CH_Deterministic_Name": crosswalk_deterministic_name,
                    "Crosswalk_CH_Name_Composite": (
                        float(crosswalk_ch.get("Name_Composite", 0.0))
                        if crosswalk_ch is not None
                        else 0.0
                    ),
                }
            )

        if local:
            local_frame = pd.DataFrame(local).sort_values(
                [
                    "Crosswalk_CH_Deterministic_Name",
                    "External_CompanyNumber_Crosswalk",
                    "Strict_Name_Exact", "Loose_Name_Exact", "Controlled_Name_Exact",
                    "Compact_Name_Exact", "PostCode_Exact", "Distinctive_Token_Overlap",
                    "Name_Composite", "External_Registry_Active", "Registry_Preference",
                ],
                ascending=[
                    False, False, False, False, False, False, False, False,
                    False, False, True,
                ],
            )
            pre_cap_count = len(local_frame)
            local_frame["Pre_Cap_Count"] = pre_cap_count
            local_frame["Candidate_Cap"] = config.max_charity_candidates_per_entity
            local_frame["Cap_Saturated"] = int(
                pre_cap_count > config.max_charity_candidates_per_entity
            )
            local_frame["Candidate_Cap_Type"] = "CHARITY"
            pair_records.extend(
                local_frame.head(config.max_charity_candidates_per_entity).to_dict("records")
            )

    pairs = pd.DataFrame(pair_records)
    LOGGER.info("Charity candidate pairs generated: %s", f"{len(pairs):,}")
    LOGGER.info(
        "Entities with at least one charity candidate: %s",
        f"{pairs['Entity_ID'].nunique() if not pairs.empty else 0:,}",
    )
    return pairs


def rank_charity_candidates(
    pairs: pd.DataFrame,
    config: PipelineConfig = CONFIG,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    output = pairs.copy()
    output["External_Legal_Name_Exact"] = output[
        "Any_Current_Name_Exact"
    ].astype(int)
    output["External_Alias_Name_Exact"] = output[
        "Any_Alias_Name_Exact"
    ].astype(int)
    output["Controlled_Or_Compact_Exact"] = output[
        "Any_Controlled_Or_Compact_Exact"
    ].astype(int)
    output["Prefix_Continuation_PostCode"] = (
        output["Max_Prefix_Containment"].ge(0.99)
        & output["PostCode_Exact"].eq(1)
        & output["Max_Distinctive_Token_Overlap"].ge(2)
    ).astype(int)
    output["Verified_Long_Prefix"] = (
        output["Blocking_Rules"].str.contains(
            r"(?:^|\|)LONG_PREFIX_CONTAINMENT(?:\||$)", na=False
        )
        & output["Max_Prefix_Containment"].ge(0.99)
        & output["Max_Distinctive_Token_Overlap"].ge(2)
    ).astype(int)
    output["Crosswalk_Auto_Eligible"] = (
        output["External_CompanyNumber_Crosswalk"].eq(1)
        & output["Crosswalk_CH_Deterministic_Name"].eq(1)
        & pd.to_numeric(
            output["Crosswalk_CH_Candidate_Rank"], errors="coerce"
        ).fillna(float("inf")).le(config.crosswalk_auto_rank_limit)
        & output["External_Registry_Active"].eq(1)
    ).astype(int)
    output["External_Evidence_Score"] = 100 * (
        0.72 * output["Name_Composite"]
        + 0.12 * output["PostCode_Exact"]
        + 0.03 * output["PostCode_Sector_Exact"]
        + 0.06 * (
            output["External_Legal_Name_Exact"] | output["External_Alias_Name_Exact"]
        ).astype(int)
        + 0.04 * output["Controlled_Or_Compact_Exact"]
        + 0.03 * output["Distinctive_Token_Overlap"].clip(upper=2) / 2
    )
    output["External_Evidence_Score"] = output["External_Evidence_Score"].clip(0, 100).round(2)
    deterministic_postcode = (
        (output["External_Legal_Name_Exact"].eq(1))
        | (output["External_Alias_Name_Exact"].eq(1))
        | (output["Controlled_Or_Compact_Exact"].eq(1))
        | (output["Prefix_Continuation_PostCode"].eq(1))
    ) & output["PostCode_Exact"].eq(1) & output["External_Registry_Active"].eq(1)
    output["External_Deterministic_PostCode"] = deterministic_postcode.astype(int)
    output["External_Deterministic_Priority"] = np.select(
        [
            output["Crosswalk_Auto_Eligible"].eq(1),
            output["External_Legal_Name_Exact"].eq(1) & output["PostCode_Exact"].eq(1),
            output["External_Alias_Name_Exact"].eq(1) & output["PostCode_Exact"].eq(1),
            output["Controlled_Or_Compact_Exact"].eq(1) & output["PostCode_Exact"].eq(1),
            output["Prefix_Continuation_PostCode"].eq(1),
            output["External_Legal_Name_Exact"].eq(1),
            output["External_Alias_Name_Exact"].eq(1),
        ],
        [1, 2, 3, 4, 5, 6, 7],
        default=9,
    ).astype(int)

    count_masks = {
        "External_Deterministic_PostCode_Count": deterministic_postcode,
        "Preferred_Deterministic_PostCode_Count": deterministic_postcode
        & output["Registry_Preference"].eq(0),
        "External_Exact_Name_Count": (
            output["External_Legal_Name_Exact"].eq(1)
            | output["External_Alias_Name_Exact"].eq(1)
        ),
        "External_Crosswalk_Active_Record_Count": output[
            "Crosswalk_Auto_Eligible"
        ].eq(1),
    }
    count_columns: list[str] = []
    for column, mask in count_masks.items():
        counts = (
            output.loc[mask]
            .groupby("Entity_ID")["Registry_Record_Key"]
            .nunique()
            .rename(column)
        )
        output = output.merge(counts, on="Entity_ID", how="left")
        count_columns.append(column)
    output[count_columns] = output[count_columns].fillna(0).astype(int)
    output = output.sort_values(
        [
            "Entity_ID", "External_Registry_Active", "External_Deterministic_Priority",
            "Registry_Preference", "External_Evidence_Score",
            "PostCode_Exact", "Name_Composite", "Registry_Record_Key",
        ],
        ascending=[True, False, True, True, False, False, False, True],
    ).copy()
    output["External_Candidate_Rank"] = output.groupby("Entity_ID").cumcount() + 1
    best = output[output["External_Candidate_Rank"].eq(1)].copy()
    second = output[output["External_Candidate_Rank"].eq(2)][
        ["Entity_ID", "External_Evidence_Score", "External_Legal_Name", "External_Registry_ID"]
    ].rename(
        columns={
            "External_Evidence_Score": "Second_Best_External_Evidence_Score",
            "External_Legal_Name": "Second_Best_External_Name",
            "External_Registry_ID": "Second_Best_External_Registry_ID",
        }
    )
    best = best.merge(second, on="Entity_ID", how="left")
    best["External_Evidence_Margin"] = (
        best["External_Evidence_Score"] - best["Second_Best_External_Evidence_Score"]
    ).round(2)
    return output, best


def decide_charity_matches(best: pd.DataFrame, config: PipelineConfig) -> pd.DataFrame:
    output = best.copy()

    def decide(row: pd.Series) -> tuple[str, str, str]:
        protected = row["Entity_Type"] in {
            "PUBLIC_BODY_LIKELY", "SOLE_TRADER_OR_PERSON_LIKELY",
            "INTERNATIONAL_ENTITY_LIKELY",
        }
        crosswalk_unique = (
            row["Crosswalk_Auto_Eligible"] == 1
            and row["External_Crosswalk_Active_Record_Count"] == 1
        )
        if crosswalk_unique and not protected:
            return (
                "EXTERNAL_AUTO_MATCH",
                "CHARITY_COMPANY_NUMBER_CROSSWALK",
                "Unique active charity registration shares the company number "
                "of a deterministic Companies House name match",
            )
        if crosswalk_unique and protected:
            return (
                "EXTERNAL_REVIEW",
                "CHARITY_COMPANY_NUMBER_ENTITY_CONFLICT",
                "Company-number crosswalk conflicts with the initial entity classification",
            )
        if (
            row["External_CompanyNumber_Crosswalk"] == 1
            and (
                row["Crosswalk_CH_Deterministic_Name"] == 1
                or row["Crosswalk_CH_Name_Composite"] >= config.strong_review_name_score
            )
        ):
            return (
                "EXTERNAL_REVIEW",
                "CHARITY_COMPANY_NUMBER_CROSSWALK_REVIEW",
                "Company-number crosswalk is plausible but not uniquely deterministic",
            )
        preferred_unique = (
            row["Registry_Preference"] == 0
            and row["Preferred_Deterministic_PostCode_Count"] == 1
        )
        globally_unique = row["External_Deterministic_PostCode_Count"] == 1
        deterministic = (
            row["External_Deterministic_PostCode"] == 1
            and bool(row["Input_PostCode_Valid"])
            and bool(row["External_Registry_Active"])
            and (preferred_unique or globally_unique)
        )
        if deterministic and not protected:
            if row["External_Legal_Name_Exact"] == 1:
                method = "EXACT_CHARITY_NAME_POSTCODE"
            elif row["External_Alias_Name_Exact"] == 1:
                method = "EXACT_CHARITY_ALIAS_POSTCODE"
            elif row["Controlled_Or_Compact_Exact"] == 1:
                method = "CONTROLLED_CHARITY_NAME_POSTCODE"
            else:
                method = "CHARITY_PREFIX_CONTINUATION_POSTCODE"
            return (
                "EXTERNAL_AUTO_MATCH", method,
                "Unique active charity registration using deterministic name and postcode evidence",
            )
        if deterministic and protected:
            return (
                "EXTERNAL_REVIEW", "ENTITY_TYPE_CONFLICT",
                "Charity evidence conflicts with the initial entity-type classification",
            )
        if protected:
            return (
                "EXTERNAL_NO_MATCH", "PROTECTED_ENTITY_SPECIALIST_ROUTE",
                "Non-deterministic charity evidence cannot override public-body, "
                "person/sole-trader or international routing",
            )
        exact_name = row["External_Legal_Name_Exact"] == 1 or row["External_Alias_Name_Exact"] == 1
        if exact_name:
            return (
                "EXTERNAL_REVIEW", "CHARITY_NAME_WITHOUT_POSTCODE",
                "Exact charity name needs confirmation because postcode evidence is absent or changed",
            )
        if row["Verified_Long_Prefix"] == 1:
            return (
                "EXTERNAL_REVIEW", "LONG_PREFIX_CONTAINMENT",
                "Verified long charity-name continuation needs human confirmation",
            )
        if (
            row["Name_Composite"] >= 0.94
            and row["Max_Distinctive_Token_Overlap"] >= 2
        ):
            return (
                "EXTERNAL_REVIEW", "STRONG_CHARITY_NAME",
                "Strong charity-register name evidence needs human confirmation",
            )
        if (
            row["Entity_Type"] in {"CHARITY_LIKELY", "EDUCATION_LIKELY"}
            and row["Name_Composite"] >= config.review_min_name_score
            and row["Max_Distinctive_Token_Overlap"] >= 2
        ):
            return (
                "EXTERNAL_REVIEW", "PLAUSIBLE_CHARITY_NAME",
                "Plausible charity-register candidate meets the review threshold",
            )
        return (
            "EXTERNAL_NO_MATCH", "LOW_CHARITY_EVIDENCE",
            "No charity candidate meets deterministic or review evidence requirements",
        )

    decisions = output.apply(decide, axis=1, result_type="expand")
    decisions.columns = [
        "External_Decision", "External_Match_Method", "External_Decision_Reason"
    ]
    return pd.concat([output, decisions], axis=1)


# %% [markdown]
# ## 7. Legacy V2 model code (retained for migration reference only)
#
# V2.3 does not call this unsupervised model. It is intentionally excluded from
# all decisions and outputs; labelled examples are required before probability
# calibration can be reintroduced.

# %%
def add_comparison_levels(pairs: pd.DataFrame) -> pd.DataFrame:
    output = pairs.copy()

    def name_level(row: pd.Series) -> int:
        if row["Strict_Name_Exact"]:
            return 5
        if row["Loose_Name_Exact"]:
            return 4
        score = max(row["Name_Jaro_Winkler"], row["Name_Levenshtein"], row["Name_Token_Sort"])
        if score >= 0.95:
            return 3
        if score >= 0.85:
            return 2
        if score >= 0.70:
            return 1
        return 0

    def token_level(value: float) -> int:
        if value >= 0.95:
            return 3
        if value >= 0.75:
            return 2
        if value >= 0.50:
            return 1
        return 0

    def prefix_level(value: float) -> int:
        if value >= 0.99:
            return 2
        if value >= 0.85:
            return 1
        return 0

    output["Name_Level"] = output.apply(name_level, axis=1).astype(int)
    output["Token_Level"] = output["Token_Jaccard"].map(token_level).astype(int)
    output["Prefix_Level"] = output["Prefix_Containment"].map(prefix_level).astype(int)
    output["PostCode_Level"] = np.select(
        [output["PostCode_Exact"].eq(1), output["PostCode_Sector_Exact"].eq(1)],
        [2, 1],
        default=0,
    ).astype(int)
    return output


EM_FEATURES = [
    "Name_Level",
    "Token_Level",
    "Prefix_Level",
    "PostCode_Level",
    "Legal_Form_Comparison",
]


def initialise_gamma(patterns: pd.DataFrame) -> np.ndarray:
    gamma = np.full(len(patterns), 0.03, dtype=float)
    exact = patterns["Name_Level"].ge(4)
    strong_name_pc = patterns["Name_Level"].ge(3) & patterns["PostCode_Level"].eq(2)
    strong_name = patterns["Name_Level"].ge(3) & patterns["Token_Level"].ge(2)
    truncated_prefix = patterns["Prefix_Level"].eq(2) & patterns["PostCode_Level"].eq(2)
    weak = patterns["Name_Level"].eq(0) & patterns["PostCode_Level"].eq(0)
    gamma[exact] = 0.97
    gamma[strong_name] = np.maximum(gamma[strong_name], 0.80)
    gamma[strong_name_pc] = np.maximum(gamma[strong_name_pc], 0.98)
    gamma[truncated_prefix] = np.maximum(gamma[truncated_prefix], 0.90)
    gamma[weak] = 0.005
    return gamma


def fit_fellegi_sunter_em(
    pairs: pd.DataFrame,
    config: PipelineConfig,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    patterns = (
        pairs.groupby(EM_FEATURES, dropna=False)
        .size()
        .reset_index(name="pattern_count")
    )
    counts = patterns["pattern_count"].astype(float).to_numpy()
    gamma = initialise_gamma(patterns)
    history: list[dict[str, float]] = []

    for iteration in range(1, config.em_max_iter + 1):
        total = counts.sum()
        match_weight = float(np.sum(counts * gamma))
        nonmatch_weight = float(np.sum(counts * (1.0 - gamma)))
        prior = float(np.clip(match_weight / total, 1e-5, 0.50))

        m_prob: dict[str, dict[Any, float]] = {}
        u_prob: dict[str, dict[Any, float]] = {}
        for feature in EM_FEATURES:
            levels = sorted(patterns[feature].dropna().unique().tolist())
            m_prob[feature], u_prob[feature] = {}, {}
            for level in levels:
                mask = patterns[feature].eq(level).to_numpy()
                m_num = float(np.sum(counts[mask] * gamma[mask]))
                u_num = float(np.sum(counts[mask] * (1.0 - gamma[mask])))
                m_prob[feature][level] = (
                    m_num + config.em_smoothing
                ) / (match_weight + config.em_smoothing * len(levels))
                u_prob[feature][level] = (
                    u_num + config.em_smoothing
                ) / (nonmatch_weight + config.em_smoothing * len(levels))

        log_match = np.full(len(patterns), math.log(prior), dtype=float)
        log_nonmatch = np.full(len(patterns), math.log(1.0 - prior), dtype=float)
        for feature in EM_FEATURES:
            for i, level in enumerate(patterns[feature]):
                log_match[i] += math.log(m_prob[feature][level])
                log_nonmatch[i] += math.log(u_prob[feature][level])

        log_odds = log_match - log_nonmatch
        new_gamma = np.where(
            log_odds >= 0,
            1.0 / (1.0 + np.exp(-log_odds)),
            np.exp(log_odds) / (1.0 + np.exp(log_odds)),
        )
        max_log = np.maximum(log_match, log_nonmatch)
        log_likelihood = float(
            np.sum(
                counts
                * (
                    max_log
                    + np.log(np.exp(log_match - max_log) + np.exp(log_nonmatch - max_log))
                )
            )
        )
        delta = float(np.max(np.abs(new_gamma - gamma)))
        history.append(
            {
                "Iteration": iteration,
                "Candidate_Prior": prior,
                "Log_Likelihood": log_likelihood,
                "Max_Gamma_Change": delta,
            }
        )
        gamma = new_gamma
        if delta < config.em_tolerance:
            break

    weights = {
        feature: {
            level: math.log(m_prob[feature][level] / u_prob[feature][level])
            for level in m_prob[feature]
        }
        for feature in EM_FEATURES
    }
    model = {"prior": prior, "m_prob": m_prob, "u_prob": u_prob, "weights": weights}
    weight_rows = [
        {
            "Feature": feature,
            "Level": level,
            "Log_Likelihood_Ratio": weight,
            "Evidence_Direction": "MATCH" if weight > 0 else "NON_MATCH" if weight < 0 else "NEUTRAL",
        }
        for feature, levels in weights.items()
        for level, weight in sorted(levels.items())
    ]
    return model, pd.DataFrame(history), pd.DataFrame(weight_rows)


def score_fs_pairs(pairs: pd.DataFrame, model: dict[str, Any]) -> pd.DataFrame:
    output = pairs.copy()
    probabilities: list[float] = []
    log_odds_values: list[float] = []
    contribution_rows: list[dict[str, float]] = []
    prior = model["prior"]

    for _, row in output.iterrows():
        log_odds = math.log(prior / (1.0 - prior))
        contributions: dict[str, float] = {}
        for feature in EM_FEATURES:
            weight = model["weights"][feature].get(row[feature], 0.0)
            contributions[feature] = weight
            log_odds += weight
        probability = (
            1.0 / (1.0 + math.exp(-log_odds))
            if log_odds >= 0
            else math.exp(log_odds) / (1.0 + math.exp(log_odds))
        )
        probabilities.append(probability)
        log_odds_values.append(log_odds)
        contribution_rows.append(contributions)

    output["Match_Probability"] = probabilities
    output["Posterior_Log_Odds"] = log_odds_values
    for feature in EM_FEATURES:
        output[f"W_{feature}"] = [row[feature] for row in contribution_rows]
    return output


# %% [markdown]
# ## 8. Deterministic Companies House evidence, ranking and governed decisions

# %%
def add_deterministic_evidence(pairs: pd.DataFrame) -> pd.DataFrame:
    """Add transparent ranking evidence. This score is not a probability."""
    output = pairs.copy()
    output["CH_Name_Is_Previous"] = output["CH_NameSource"].str.startswith("PREVIOUS").astype(int)
    output["Current_Name_Exact"] = (
        output["CH_NameSource"].eq("CURRENT")
        & (output["Strict_Name_Exact"].eq(1) | output["Loose_Name_Exact"].eq(1))
    ).astype(int)
    output["Previous_Name_Exact"] = (
        output["CH_Name_Is_Previous"].eq(1)
        & (output["Strict_Name_Exact"].eq(1) | output["Loose_Name_Exact"].eq(1))
    ).astype(int)
    output["Controlled_Or_Compact_Exact"] = (
        output["Controlled_Name_Exact"].eq(1) | output["Compact_Name_Exact"].eq(1)
    ).astype(int)
    output["Evidence_Score"] = 100 * (
        0.72 * output["Name_Composite"]
        + 0.10 * output["PostCode_Exact"]
        + 0.03 * output["PostCode_Sector_Exact"]
        + 0.06 * (output["Current_Name_Exact"] | output["Previous_Name_Exact"]).astype(int)
        + 0.04 * output["Controlled_Or_Compact_Exact"]
        + 0.03 * output["Distinctive_Token_Overlap"].clip(upper=2) / 2
        + 0.02 * output["Truncated_Prefix_PostCode"]
    )
    output["Evidence_Score"] = output["Evidence_Score"].clip(0, 100).round(2)
    return output


def rank_candidates(pairs: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    output = pairs.copy()
    output["Deterministic_Priority"] = np.select(
        [
            output["Current_Name_Exact"].eq(1) & output["PostCode_Exact"].eq(1),
            output["Previous_Name_Exact"].eq(1) & output["PostCode_Exact"].eq(1),
            output["Controlled_Or_Compact_Exact"].eq(1) & output["PostCode_Exact"].eq(1),
            output["Truncated_Prefix_PostCode"].eq(1),
            output["Current_Name_Exact"].eq(1),
            output["Previous_Name_Exact"].eq(1),
            output["Controlled_Or_Compact_Exact"].eq(1),
        ],
        [1, 2, 3, 4, 5, 6, 7],
        default=9,
    ).astype(int)

    count_specs = {
        "Exact_Current_PostCode_Count": output["Current_Name_Exact"].eq(1) & output["PostCode_Exact"].eq(1),
        "Exact_Previous_PostCode_Count": output["Previous_Name_Exact"].eq(1) & output["PostCode_Exact"].eq(1),
        "Controlled_PostCode_Count": output["Controlled_Or_Compact_Exact"].eq(1) & output["PostCode_Exact"].eq(1),
        "Truncated_Prefix_PostCode_Count": output["Truncated_Prefix_PostCode"].eq(1),
        "Exact_Current_Name_Count": output["Current_Name_Exact"].eq(1),
        "Exact_Previous_Name_Count": output["Previous_Name_Exact"].eq(1),
        "Any_Exact_Name_Count": output["Current_Name_Exact"].eq(1) | output["Previous_Name_Exact"].eq(1),
    }
    count_columns: list[str] = []
    for column, mask in count_specs.items():
        counts = (
            output.loc[mask]
            .groupby("Entity_ID")["CH_CompanyNumber"]
            .nunique()
            .rename(column)
        )
        output = output.merge(counts, on="Entity_ID", how="left")
        count_columns.append(column)
    output[count_columns] = output[count_columns].fillna(0).astype(int)
    output["Alias_Rank_Priority"] = output["Best_Alias_Role"].map(
        {"REGISTERED_NAME": 1, "LEGAL_CORE": 1, "WHOLE_NAME": 2, "TRADING_NAME": 3}
    ).fillna(4).astype(int)

    output = output.sort_values(
        [
            "Entity_ID", "Deterministic_Priority", "Evidence_Score",
            "Alias_Rank_Priority", "PostCode_Exact", "Name_Composite",
            "CH_Status_Active", "CH_CompanyNumber",
        ],
        ascending=[True, True, False, True, False, False, False, True],
    ).copy()
    output["Candidate_Rank"] = output.groupby("Entity_ID").cumcount() + 1

    best = output[output["Candidate_Rank"].eq(1)].copy()
    second = (
        output[output["Candidate_Rank"].eq(2)][
            ["Entity_ID", "Evidence_Score", "CH_CompanyName", "CH_CompanyNumber"]
        ]
        .rename(
            columns={
                "Evidence_Score": "Second_Best_Evidence_Score",
                "CH_CompanyName": "Second_Best_CompanyName",
                "CH_CompanyNumber": "Second_Best_CompanyNumber",
            }
        )
    )
    best = best.merge(second, on="Entity_ID", how="left")
    best["Evidence_Margin"] = (
        best["Evidence_Score"] - best["Second_Best_Evidence_Score"]
    ).round(2)
    return output, best


def decide_best_matches(best: pd.DataFrame, config: PipelineConfig) -> pd.DataFrame:
    output = best.copy()

    route_map = {
        "PUBLIC_BODY_LIKELY": "PUBLIC_BODY_REGISTRY",
        "CHARITY_LIKELY": "CHARITY_REGISTRY",
        "EDUCATION_LIKELY": "EDUCATION_REGISTRY",
        "SOLE_TRADER_OR_PERSON_LIKELY": "SOLE_TRADER_OR_SOURCE_REVIEW",
        "INTERNATIONAL_ENTITY_LIKELY": "INTERNATIONAL_OR_SOURCE_REVIEW",
    }

    def decide(row: pd.Series) -> tuple[str, str, str, str]:
        entity_type = row["Entity_Type"]
        external_only = {
            "PUBLIC_BODY_LIKELY",
            "SOLE_TRADER_OR_PERSON_LIKELY",
            "INTERNATIONAL_ENTITY_LIKELY",
        }
        if entity_type in external_only or not bool(row["Input_PostCode_Valid"]):
            route = route_map.get(entity_type, "NON_UK_OR_INVALID_POSTCODE_REVIEW")
            return (
                "ROUTE_EXTERNAL",
                "ENTITY_SPECIFIC_ROUTE",
                "Entity type or postcode is outside deterministic UK company auto-linking",
                route,
            )

        protected_review = entity_type in {"CHARITY_LIKELY", "EDUCATION_LIKELY"}
        exact_current_pc = row["Any_Exact_Name_Count"] == 1 and row["Exact_Current_PostCode_Count"] == 1 and row["Current_Name_Exact"] == 1 and row["PostCode_Exact"] == 1
        exact_previous_pc = row["Any_Exact_Name_Count"] == 1 and row["Exact_Previous_PostCode_Count"] == 1 and row["Previous_Name_Exact"] == 1 and row["PostCode_Exact"] == 1
        controlled_pc = row["Controlled_PostCode_Count"] == 1 and row["Controlled_Or_Compact_Exact"] == 1 and row["PostCode_Exact"] == 1
        unique_truncated_pc = row["Truncated_Prefix_PostCode_Count"] == 1 and row["Truncated_Prefix_PostCode"] == 1

        if protected_review:
            if exact_current_pc or exact_previous_pc or row["Name_Composite"] >= config.review_min_name_score:
                return (
                    "REVIEW",
                    "ENTITY_SPECIFIC_REVIEW",
                    "Plausible incorporated candidate for a charity or education entity",
                    route_map[entity_type],
                )
            return (
                "ROUTE_EXTERNAL",
                "ENTITY_SPECIFIC_ROUTE",
                "No deterministic incorporated-company evidence; use the specialist registry",
                route_map[entity_type],
            )

        if row["Best_Alias_Role"] == "TRADING_NAME":
            if row["Name_Composite"] >= config.review_min_name_score or row["Controlled_Or_Compact_Exact"] == 1:
                return (
                    "REVIEW",
                    "TRADING_ALIAS_REVIEW",
                    "Only the trading alias supports this candidate",
                    "COMPANIES_HOUSE",
                )
            return ("NO_MATCH", "LOW_EVIDENCE", "Trading alias evidence is insufficient", "COMPANIES_HOUSE")

        if exact_current_pc:
            return ("AUTO_MATCH", "EXACT_CURRENT_NAME_POSTCODE", "Unique exact current legal name plus exact postcode", "COMPANIES_HOUSE")
        if exact_previous_pc:
            return ("AUTO_MATCH", "EXACT_PREVIOUS_NAME_POSTCODE", "Unique exact historical Companies House name plus exact postcode", "COMPANIES_HOUSE")

        unique_exact_current = (
            row["Exact_Current_Name_Count"] == 1
            and row["Any_Exact_Name_Count"] == 1
            and row["Current_Name_Exact"] == 1
            and row["Recipient_Distinctive_Token_Count"] >= config.exact_name_min_distinctive_tokens
            and row["Distinctive_Token_Overlap"] >= config.exact_name_min_distinctive_tokens
        )
        if unique_exact_current:
            return ("AUTO_MATCH", "UNIQUE_EXACT_CURRENT_NAME", "Unique exact current legal name with at least two distinctive tokens", "COMPANIES_HOUSE")
        if unique_truncated_pc:
            return ("AUTO_MATCH", "TRUNCATED_PREFIX_POSTCODE", "Unique confirmed truncated-prefix continuation plus exact postcode", "COMPANIES_HOUSE")
        if controlled_pc:
            return ("AUTO_MATCH", "CONTROLLED_ABBREVIATION_POSTCODE", "Unique controlled abbreviation expansion plus exact postcode", "COMPANIES_HOUSE")

        if row["Exact_Current_Name_Count"] > 1 or row["Exact_Previous_Name_Count"] > 1:
            return ("REVIEW", "MULTIPLE_EXACT_NAMES", "More than one exact legal-name candidate exists", "COMPANIES_HOUSE")
        if row["Previous_Name_Exact"] == 1:
            return ("REVIEW", "PREVIOUS_NAME_WITHOUT_POSTCODE", "Historical Companies House name matches without postcode confirmation", "COMPANIES_HOUSE")
        if row["Name_Composite"] >= config.strong_review_name_score:
            return ("REVIEW", "STRONG_NONEXACT_NAME", "Strong registered alias match needs human confirmation", "COMPANIES_HOUSE")
        if row["Name_Composite"] >= config.review_min_name_score and row["Distinctive_Token_Overlap"] >= 1:
            return ("REVIEW", "PLAUSIBLE_NAME_EVIDENCE", "Plausible candidate meets the minimum review evidence", "COMPANIES_HOUSE")
        return ("NO_MATCH", "LOW_EVIDENCE", "No candidate meets deterministic or review evidence requirements", "COMPANIES_HOUSE")

    decisions = output.apply(decide, axis=1, result_type="expand")
    decisions.columns = ["Decision", "Match_Method", "Decision_Reason", "Registry_Route"]
    output = pd.concat([output, decisions], axis=1)
    return output


def create_no_candidate_rows(
    entities: pd.DataFrame,
    best: pd.DataFrame,
) -> pd.DataFrame:
    missing = entities[~entities["Entity_ID"].isin(best["Entity_ID"])].copy()
    if missing.empty:
        return missing
    routable = {
        "PUBLIC_BODY_LIKELY",
        "EDUCATION_LIKELY",
        "CHARITY_LIKELY",
        "SOLE_TRADER_OR_PERSON_LIKELY",
        "INTERNATIONAL_ENTITY_LIKELY",
    }
    missing["Decision"] = np.where(
        missing["Entity_Type"].isin(routable) | ~missing["Input_PostCode_Valid"],
        "ROUTE_EXTERNAL",
        "NO_MATCH",
    )
    missing["Match_Method"] = "NO_CANDIDATE"
    missing["Decision_Reason"] = np.where(
        missing["Decision"].eq("ROUTE_EXTERNAL"),
        "No CH candidate; route to the appropriate external registry",
        "No Companies House candidate passed blocking and minimum score rules",
    )
    missing["Registry_Route"] = missing["Entity_Type"].map(
        {
            "PUBLIC_BODY_LIKELY": "PUBLIC_BODY_REGISTRY",
            "EDUCATION_LIKELY": "EDUCATION_REGISTRY",
            "CHARITY_LIKELY": "CHARITY_REGISTRY",
            "SOLE_TRADER_OR_PERSON_LIKELY": "SOLE_TRADER_OR_SOURCE_REVIEW",
            "INTERNATIONAL_ENTITY_LIKELY": "INTERNATIONAL_OR_SOURCE_REVIEW",
        }
    ).fillna("NON_UK_OR_INVALID_POSTCODE_REVIEW")
    return missing


# %% [markdown]
# ## 9. Preserve all input columns, build QA tables and export

# %%
CROSSWALK_COLUMNS = [
    "Entity_ID", "Entity_Type", "Input_PostCode_Valid", "Source_Name_Possibly_Truncated",
    "CH_CompanyName", "CH_CompanyNumber", "CH_AddressLine1", "CH_PostTown",
    "CH_PostCode", "CH_CompanyStatus", "CH_MatchedName", "CH_NameSource",
    "CH_PreviousNameDate", "Best_Alias_Role", "Best_Alias", "Evidence_Score",
    "Second_Best_Evidence_Score", "Evidence_Margin", "Name_Composite",
    "Name_Jaro_Winkler", "Name_Levenshtein", "Name_Token_Sort",
    "Name_Token_Set", "Token_Jaccard", "Prefix_Containment", "PostCode_Exact",
    "PostCode_Sector_Exact", "Current_Name_Exact", "Previous_Name_Exact",
    "Controlled_Or_Compact_Exact", "Distinctive_Token_Overlap",
    "Recipient_Distinctive_Token_Count", "Decision", "Match_Method",
    "Decision_Reason", "Registry_Route",
]


def build_entity_crosswalk(
    entities: pd.DataFrame,
    decided_best: pd.DataFrame,
    no_candidate: pd.DataFrame,
) -> pd.DataFrame:
    available = [column for column in CROSSWALK_COLUMNS if column in decided_best.columns]
    selected = decided_best[available].copy()
    base = entities[
        [
            "Entity_ID", "Recipient_Name", "Recipient_PostCode_Raw",
            "Recipient_PostCode", "Input_PostCode_Valid", "Entity_Type",
            "Source_Name_Possibly_Truncated",
        ]
    ].copy()
    crosswalk = base.merge(
        selected.drop(
            columns=[
                column
                for column in [
                    "Entity_Type", "Input_PostCode_Valid",
                    "Source_Name_Possibly_Truncated",
                ]
                if column in selected.columns
            ]
        ),
        on="Entity_ID",
        how="left",
        validate="one_to_one",
    )
    if not no_candidate.empty:
        no_candidate_fields = no_candidate[
            ["Entity_ID", "Decision", "Match_Method", "Decision_Reason", "Registry_Route"]
        ].set_index("Entity_ID")
        missing_mask = crosswalk["Decision"].isna()
        for column in ["Decision", "Match_Method", "Decision_Reason", "Registry_Route"]:
            crosswalk.loc[missing_mask, column] = crosswalk.loc[
                missing_mask, "Entity_ID"
            ].map(no_candidate_fields[column])
    return crosswalk


def combine_charity_results(
    crosswalk: pd.DataFrame,
    decided_charity_best: pd.DataFrame,
) -> pd.DataFrame:
    """Combine CH and charity evidence without leaking unapproved identifiers."""
    output = crosswalk.copy()
    output["Initial_Entity_Type"] = output["Entity_Type"]

    ch_candidate_map = {
        "CH_CompanyName": "Best_CH_Candidate_CompanyName",
        "CH_CompanyNumber": "Best_CH_Candidate_CompanyNumber",
        "CH_AddressLine1": "Best_CH_Candidate_AddressLine1",
        "CH_PostTown": "Best_CH_Candidate_PostTown",
        "CH_PostCode": "Best_CH_Candidate_PostCode",
        "CH_CompanyStatus": "Best_CH_Candidate_Status",
        "CH_MatchedName": "Best_CH_Candidate_MatchedName",
        "CH_NameSource": "Best_CH_Candidate_NameSource",
    }
    for source, target in ch_candidate_map.items():
        output[target] = output[source] if source in output.columns else pd.NA

    if decided_charity_best.empty:
        selected_ch = output["Decision"].eq("AUTO_MATCH") & output["Registry_Route"].eq(
            "COMPANIES_HOUSE"
        )
        for source in ch_candidate_map:
            if source in output.columns:
                output.loc[~selected_ch, source] = pd.NA
        return output

    candidate_columns = {
        "Registry_Record_Key": "Best_External_Candidate_Record_Key",
        "External_Registry": "Best_External_Candidate_Registry",
        "External_Jurisdiction": "Best_External_Candidate_Jurisdiction",
        "External_Registry_ID": "Best_External_Candidate_Registry_ID",
        "External_Registry_Display_ID": "Best_External_Candidate_Display_ID",
        "External_Linked_ID": "Best_External_Candidate_Linked_ID",
        "External_Legal_Name": "Best_External_Candidate_Legal_Name",
        "External_Known_As": "Best_External_Candidate_Known_As",
        "External_PostCode": "Best_External_Candidate_PostCode",
        "External_Address": "Best_External_Candidate_Address",
        "External_Status": "Best_External_Candidate_Status",
        "External_CompanyNumber": "Best_External_Candidate_CompanyNumber",
        "External_Constitutional_Form": "Best_External_Candidate_Constitutional_Form",
        "External_Website": "Best_External_Candidate_Website",
        "External_MatchedName": "Best_External_Candidate_MatchedName",
        "External_NameSource": "Best_External_Candidate_NameSource",
        "External_Evidence_Score": "Best_External_Candidate_Evidence_Score",
        "External_Evidence_Margin": "External_Evidence_Margin",
        "Name_Composite": "External_Name_Composite",
        "PostCode_Exact": "External_PostCode_Exact",
        "External_Decision": "External_Candidate_Decision",
        "External_Match_Method": "External_Candidate_Match_Method",
        "External_Decision_Reason": "External_Candidate_Decision_Reason",
        "External_CompanyNumber_Crosswalk": "External_Candidate_CompanyNumber_Crosswalk",
        "Crosswalk_CH_Candidate_Rank": "External_Candidate_Crosswalk_CH_Rank",
        "Crosswalk_CH_Current_Name_Exact": "External_Candidate_Crosswalk_Current_Exact",
        "Crosswalk_CH_Previous_Name_Exact": "External_Candidate_Crosswalk_Previous_Exact",
        "Crosswalk_CH_Truncated_Prefix_PostCode": "External_Candidate_Crosswalk_Prefix_PostCode",
        "Crosswalk_CH_Name_Composite": "External_Candidate_Crosswalk_CH_Name_Score",
        "External_Crosswalk_Active_Record_Count": "External_Candidate_Crosswalk_Active_Count",
        "Verified_Long_Prefix": "External_Candidate_Verified_Long_Prefix",
    }
    available = ["Entity_ID"] + [
        column for column in candidate_columns if column in decided_charity_best.columns
    ]
    external = decided_charity_best[available].rename(columns=candidate_columns)
    output = output.merge(external, on="Entity_ID", how="left", validate="one_to_one")

    auto_external = output["External_Candidate_Decision"].eq("EXTERNAL_AUTO_MATCH")
    review_external = output["External_Candidate_Decision"].eq("EXTERNAL_REVIEW")
    accepted_external = auto_external | review_external
    entity_type_conflict = output["External_Candidate_Match_Method"].isin(
        {"ENTITY_TYPE_CONFLICT", "CHARITY_COMPANY_NUMBER_ENTITY_CONFLICT"}
    )
    output.loc[accepted_external & ~entity_type_conflict, "Entity_Type"] = (
        "CHARITY_LIKELY"
    )
    output.loc[auto_external, "Decision"] = "EXTERNAL_AUTO_MATCH"
    output.loc[review_external, "Decision"] = "EXTERNAL_REVIEW"
    output.loc[accepted_external, "Match_Method"] = output.loc[
        accepted_external, "External_Candidate_Match_Method"
    ]
    output.loc[accepted_external, "Decision_Reason"] = output.loc[
        accepted_external, "External_Candidate_Decision_Reason"
    ]
    output.loc[accepted_external, "Registry_Route"] = (
        "CHARITY_REGISTRY:"
        + output.loc[accepted_external, "Best_External_Candidate_Registry"].astype("string")
    )

    selected_external_map = {
        "External_Registry": "Best_External_Candidate_Registry",
        "External_Registry_ID": "Best_External_Candidate_Registry_ID",
        "External_Registry_Display_ID": "Best_External_Candidate_Display_ID",
        "External_Linked_ID": "Best_External_Candidate_Linked_ID",
        "External_Legal_Name": "Best_External_Candidate_Legal_Name",
        "External_PostCode": "Best_External_Candidate_PostCode",
        "External_Address": "Best_External_Candidate_Address",
        "External_Status": "Best_External_Candidate_Status",
        "External_CompanyNumber": "Best_External_Candidate_CompanyNumber",
        "External_Match_Basis": "External_Candidate_Match_Method",
    }
    for selected, candidate in selected_external_map.items():
        output[selected] = pd.NA
        output.loc[auto_external, selected] = output.loc[auto_external, candidate]

    output["Registry_Suggested_Entity_Type"] = np.where(
        accepted_external, "CHARITY_LIKELY", pd.NA
    )
    output["Dual_Registry_Company_Confirmed"] = (
        output["Best_External_Candidate_CompanyNumber"].notna()
        & output["Best_CH_Candidate_CompanyNumber"].notna()
        & output["Best_External_Candidate_CompanyNumber"].eq(
            output["Best_CH_Candidate_CompanyNumber"]
        )
    )

    selected_ch = output["Decision"].eq("AUTO_MATCH") & output["Registry_Route"].eq(
        "COMPANIES_HOUSE"
    )
    for source in ch_candidate_map:
        if source in output.columns:
            output.loc[~selected_ch, source] = pd.NA
    return output


def enrich_original_rows(
    full: pd.DataFrame,
    row_entity_map: pd.DataFrame,
    crosswalk: pd.DataFrame,
) -> pd.DataFrame:
    match_columns = [
        column for column in crosswalk.columns
        if column not in {"Recipient_Name", "Recipient_PostCode_Raw", "Recipient_PostCode"}
    ]
    enriched = full.merge(
        row_entity_map,
        on="_Input_Row_ID",
        how="left",
        validate="one_to_one",
    ).merge(
        crosswalk[match_columns],
        on="Entity_ID",
        how="left",
        validate="many_to_one",
    )
    invalid = enriched["Entity_ID"].isna()
    enriched.loc[invalid, "Decision"] = "INVALID_INPUT"
    enriched.loc[invalid, "Match_Method"] = "MISSING_NAME"
    enriched.loc[invalid, "Decision_Reason"] = "Recipient name is blank"
    return enriched.sort_values("_Input_Row_ID").reset_index(drop=True)


def build_qa_summary(
    full: pd.DataFrame,
    entities: pd.DataFrame,
    ch_candidates: pd.DataFrame,
    candidate_pairs: pd.DataFrame,
    crosswalk: pd.DataFrame,
    rows_scanned: int,
    charity_registries: pd.DataFrame,
    charity_candidate_pairs: pd.DataFrame,
) -> pd.DataFrame:
    decision_counts = crosswalk["Decision"].value_counts(dropna=False)
    type_counts = crosswalk["Entity_Type"].value_counts(dropna=False)
    metrics: list[tuple[str, Any]] = [
        ("Input rows", len(full)),
        ("Unique name+postcode entities", len(entities)),
        ("Companies House rows scanned", rows_scanned),
        ("Unique CH candidates retained", len(ch_candidates)),
        ("Entity-company candidate pairs", len(candidate_pairs)),
        ("Entities with at least one candidate", candidate_pairs["Entity_ID"].nunique() if not candidate_pairs.empty else 0),
        ("Original row count preserved", len(full)),
        ("Invalid or non-UK postcodes", int((~entities["Input_PostCode_Valid"]).sum())),
        ("Unsupervised probability auto-matches", 0),
        ("Charity registry records loaded", len(charity_registries)),
        ("Entity-charity candidate pairs", len(charity_candidate_pairs)),
        (
            "Entities with at least one charity candidate",
            charity_candidate_pairs["Entity_ID"].nunique()
            if not charity_candidate_pairs.empty else 0,
        ),
        (
            "CH candidate-cap saturated entities",
            int(
                candidate_pairs.loc[
                    candidate_pairs.get("Cap_Saturated", 0).eq(1), "Entity_ID"
                ].nunique()
            )
            if not candidate_pairs.empty and "Cap_Saturated" in candidate_pairs
            else 0,
        ),
        (
            "Charity candidate-cap saturated entities",
            int(
                charity_candidate_pairs.loc[
                    charity_candidate_pairs.get("Cap_Saturated", 0).eq(1), "Entity_ID"
                ].nunique()
            )
            if not charity_candidate_pairs.empty and "Cap_Saturated" in charity_candidate_pairs
            else 0,
        ),
    ]
    metrics.extend((f"Decision: {key}", value) for key, value in decision_counts.items())
    metrics.extend((f"Entity type: {key}", value) for key, value in type_counts.items())
    if rows_scanned and len(entities):
        possible = rows_scanned * len(entities)
        metrics.append(("Candidate reduction ratio", 1 - len(candidate_pairs) / possible))
    return pd.DataFrame(metrics, columns=["Metric", "Value"])


def config_table(config: PipelineConfig) -> pd.DataFrame:
    values = asdict(config)
    values["base_folder"] = str(values["base_folder"])
    values.update(
        {
            "decision_note": "V2.3 uses only explicit deterministic auto-match rules. CH and charity evidence scores are ranking aids, not probabilities.",
        }
    )
    return pd.DataFrame({"Parameter": values.keys(), "Value": values.values()})


def decision_rules_table() -> pd.DataFrame:
    rows = [
        ("AUTO_MATCH", "EXACT_CURRENT_NAME_POSTCODE", "Unique exact current legal name plus exact postcode"),
        ("AUTO_MATCH", "EXACT_PREVIOUS_NAME_POSTCODE", "Unique exact historical Companies House name plus exact postcode"),
        ("AUTO_MATCH", "UNIQUE_EXACT_CURRENT_NAME", "Unique exact current legal name with at least two distinctive tokens"),
        ("AUTO_MATCH", "TRUNCATED_PREFIX_POSTCODE", "Unique source-truncated prefix continuation plus exact postcode"),
        ("AUTO_MATCH", "CONTROLLED_ABBREVIATION_POSTCODE", "Unique controlled abbreviation plus exact postcode"),
        ("REVIEW", "TRADING_ALIAS_REVIEW", "Only the trading alias supports the candidate"),
        ("REVIEW", "PREVIOUS_NAME_WITHOUT_POSTCODE", "Historical name matches without postcode confirmation"),
        ("REVIEW", "MULTIPLE_EXACT_NAMES", "More than one exact legal-name candidate exists"),
        ("REVIEW", "ENTITY_SPECIFIC_REVIEW", "Charity or education candidate may be incorporated"),
        ("ROUTE_EXTERNAL", "ENTITY_SPECIFIC_ROUTE", "Public body, international, sole-trader/person or invalid/non-UK postcode"),
        ("NO_MATCH", "LOW_EVIDENCE", "No candidate meets deterministic or review evidence requirements"),
        ("EXTERNAL_AUTO_MATCH", "EXACT_CHARITY_NAME_POSTCODE", "Unique active charity legal name plus exact postcode"),
        ("EXTERNAL_AUTO_MATCH", "EXACT_CHARITY_ALIAS_POSTCODE", "Unique active known-as charity name plus exact postcode"),
        ("EXTERNAL_AUTO_MATCH", "CHARITY_PREFIX_CONTINUATION_POSTCODE", "Unique charity-name continuation plus exact postcode"),
        ("EXTERNAL_AUTO_MATCH", "CHARITY_COMPANY_NUMBER_CROSSWALK", "Unique active charity record linked to a deterministic CH name match by company number"),
        ("EXTERNAL_REVIEW", "CHARITY_NAME_WITHOUT_POSTCODE", "Exact charity name without confirmed postcode"),
        ("EXTERNAL_REVIEW", "LONG_PREFIX_CONTAINMENT", "Verified long charity-name continuation without deterministic postcode evidence"),
        ("EXTERNAL_REVIEW", "CHARITY_COMPANY_NUMBER_CROSSWALK_REVIEW", "Non-unique or otherwise non-deterministic company-number crosswalk"),
        ("EXTERNAL_REVIEW", "STRONG_CHARITY_NAME", "Strong non-exact charity-register candidate"),
        ("EXTERNAL_NO_MATCH", "LOW_CHARITY_EVIDENCE", "No charity candidate meets the review threshold"),
    ]
    return pd.DataFrame(rows, columns=["Decision", "Method", "Rule"])


def export_outputs(
    config: PipelineConfig,
    enriched: pd.DataFrame,
    crosswalk: pd.DataFrame,
    ranked_pairs: pd.DataFrame,
    qa_summary: pd.DataFrame,
    input_qa_summary: pd.DataFrame,
    reference_comparison: pd.DataFrame,
    ranked_charity_pairs: pd.DataFrame,
) -> None:
    config.output_folder.mkdir(parents=True, exist_ok=True)
    ranked_pairs.to_csv(config.all_candidates_file, index=False, compression="gzip")
    ranked_charity_pairs.to_csv(
        config.all_charity_candidates_file, index=False, compression="gzip"
    )

    diagnostics = (
        ranked_pairs[ranked_pairs["Candidate_Rank"].le(config.diagnostics_top_n)].copy()
        if "Candidate_Rank" in ranked_pairs.columns
        else ranked_pairs.copy()
    )
    review_ids = set(crosswalk.loc[crosswalk["Decision"].eq("REVIEW"), "Entity_ID"])
    review_diagnostics = (
        diagnostics[diagnostics["Entity_ID"].isin(review_ids)].copy()
        if "Entity_ID" in diagnostics.columns
        else diagnostics.copy()
    )
    charity_diagnostics = (
        ranked_charity_pairs[
            ranked_charity_pairs["External_Candidate_Rank"].le(config.diagnostics_top_n)
        ].copy()
        if "External_Candidate_Rank" in ranked_charity_pairs.columns
        else ranked_charity_pairs.copy()
    )
    saturation_frames: list[pd.DataFrame] = []
    for frame in (ranked_pairs, ranked_charity_pairs):
        if frame.empty or "Cap_Saturated" not in frame.columns:
            continue
        columns = [
            column
            for column in [
                "Entity_ID", "Recipient_Name", "Candidate_Cap_Type",
                "Pre_Cap_Count", "Candidate_Cap", "Cap_Saturated",
            ]
            if column in frame.columns
        ]
        saturated = frame.loc[frame["Cap_Saturated"].eq(1), columns].drop_duplicates()
        if not saturated.empty:
            saturation_frames.append(saturated)
    cap_saturation = (
        pd.concat(saturation_frames, ignore_index=True)
        if saturation_frames
        else pd.DataFrame(
            columns=[
                "Entity_ID", "Recipient_Name", "Candidate_Cap_Type",
                "Pre_Cap_Count", "Candidate_Cap", "Cap_Saturated",
            ]
        )
    )
    for column in [
        "Manual_Label_MATCH_NON_MATCH_UNSURE",
        "Manual_Selected_CompanyNumber",
        "Reviewer",
        "Reviewer_Notes",
    ]:
        review_diagnostics[column] = ""

    sheets = {
        "Enriched_Awards": enriched,
        "Entity_Crosswalk": crosswalk,
        "Auto_Matches": crosswalk[crosswalk["Decision"].eq("AUTO_MATCH")],
        "External_Auto_Matches": crosswalk[
            crosswalk["Decision"].eq("EXTERNAL_AUTO_MATCH")
        ],
        "Review": review_diagnostics,
        "External_Review": crosswalk[crosswalk["Decision"].eq("EXTERNAL_REVIEW")],
        "No_Match": crosswalk[crosswalk["Decision"].eq("NO_MATCH")],
        "External_Routing": crosswalk[crosswalk["Decision"].eq("ROUTE_EXTERNAL")],
        "Top_Candidates": diagnostics,
        "Charity_Top_Candidates": charity_diagnostics,
        "Cap_Saturation": cap_saturation,
        "QA_Summary": qa_summary,
        "Input_QA": input_qa_summary,
        "Reference_Check": reference_comparison,
        "Parameters": config_table(config),
        "Decision_Rules": decision_rules_table(),
    }

    with pd.ExcelWriter(config.workbook_file, engine="openpyxl") as writer:
        for sheet_name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=sheet_name[:31], index=False)
            worksheet = writer.book[sheet_name[:31]]
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            if len(frame) <= 10_000:
                for column_cells in worksheet.iter_cols(
                    min_row=1,
                    max_row=min(worksheet.max_row, 500),
                    max_col=worksheet.max_column,
                ):
                    letter = column_cells[0].column_letter
                    width = min(
                        45,
                        max(10, max(len(_text(cell.value)) for cell in column_cells) + 2),
                    )
                    worksheet.column_dimensions[letter].width = width

    LOGGER.info("QA workbook: %s", config.workbook_file)
    LOGGER.info("Top-candidate audit file: %s", config.all_candidates_file)
    LOGGER.info("Top-charity-candidate audit file: %s", config.all_charity_candidates_file)


# %% [markdown]
# ## 10. End-to-end runner

# %%
def validate_config(config: PipelineConfig) -> None:
    if not config.grants_file.exists():
        raise FileNotFoundError(f"Grant input not found: {config.grants_file}")
    if not config.companies_house_file.exists():
        raise FileNotFoundError(
            f"Companies House input not found: {config.companies_house_file}"
        )
    if config.use_charity_registries:
        missing_charity_files = [
            path for path in [
                config.charity_ew_file,
                config.charity_scotland_file,
                config.charity_ni_file,
            ]
            if not path.exists()
        ]
        if missing_charity_files:
            raise FileNotFoundError(
                "Charity registry input(s) not found: "
                + ", ".join(str(path) for path in missing_charity_files)
            )
    if not 0 <= config.review_min_name_score <= config.strong_review_name_score <= 1:
        raise ValueError("Name thresholds must satisfy 0 <= REVIEW <= STRONG_REVIEW <= 1")
    if config.max_candidates_per_entity < config.diagnostics_top_n:
        raise ValueError("max_candidates_per_entity must be >= diagnostics_top_n")
    if config.max_charity_candidates_per_entity < config.diagnostics_top_n:
        raise ValueError(
            "max_charity_candidates_per_entity must be >= diagnostics_top_n"
        )
    config.output_folder.mkdir(parents=True, exist_ok=True)


def run_pipeline(config: PipelineConfig = CONFIG) -> dict[str, Any]:
    validate_config(config)
    started = time.perf_counter()
    LOGGER.info("Starting multi-registry entity resolution V2.3")

    (
        full,
        row_entity_map,
        entities,
        aliases,
        input_qa_summary,
        reference_comparison,
        alignment_error,
    ) = prepare_input(config)
    if alignment_error and config.stop_on_alignment_error:
        with pd.ExcelWriter(config.preflight_file, engine="openpyxl") as writer:
            input_qa_summary.to_excel(writer, sheet_name="Input_QA", index=False)
            reference_comparison.to_excel(writer, sheet_name="Reference_Check", index=False)
        raise RuntimeError(
            "Input QA detected a likely one-row postcode shift. The Companies House "
            f"scan was not started. Inspect and correct {config.preflight_file}."
        )

    ch_candidates, rows_scanned = scan_companies_house(aliases, entities, config)
    candidate_pairs = generate_candidate_pairs(entities, aliases, ch_candidates, config)

    if candidate_pairs.empty:
        ranked_pairs = pd.DataFrame()
        decided_best = pd.DataFrame(columns=["Entity_ID"])
    else:
        candidate_pairs = add_deterministic_evidence(candidate_pairs)
        ranked_pairs, best = rank_candidates(candidate_pairs)
        decided_best = decide_best_matches(best, config)
    no_candidate = create_no_candidate_rows(entities, decided_best)
    crosswalk = build_entity_crosswalk(entities, decided_best, no_candidate)
    charity_registries = load_charity_registries(config)
    charity_candidate_pairs = generate_charity_candidate_pairs(
        entities, aliases, charity_registries, config, ranked_pairs
    )
    if charity_candidate_pairs.empty:
        ranked_charity_pairs = pd.DataFrame()
        decided_charity_best = pd.DataFrame(columns=["Entity_ID"])
    else:
        ranked_charity_pairs, charity_best = rank_charity_candidates(
            charity_candidate_pairs, config
        )
        decided_charity_best = decide_charity_matches(charity_best, config)
    crosswalk = combine_charity_results(crosswalk, decided_charity_best)
    enriched = enrich_original_rows(full, row_entity_map, crosswalk)

    if len(enriched) != len(full):
        raise AssertionError("Output row count differs from original input row count")
    if enriched["_Input_Row_ID"].duplicated().any():
        raise AssertionError("Input row IDs are no longer unique")

    qa_summary = build_qa_summary(
        full, entities, ch_candidates, ranked_pairs, crosswalk, rows_scanned,
        charity_registries, ranked_charity_pairs,
    )
    export_outputs(
        config,
        enriched,
        crosswalk,
        ranked_pairs,
        qa_summary,
        input_qa_summary,
        reference_comparison,
        ranked_charity_pairs,
    )

    elapsed_minutes = (time.perf_counter() - started) / 60
    LOGGER.info("Completed V2.3 in %.2f minutes", elapsed_minutes)
    LOGGER.info("Decision counts:\n%s", crosswalk["Decision"].value_counts(dropna=False))

    return {
        "enriched": enriched,
        "crosswalk": crosswalk,
        "ranked_candidates": ranked_pairs,
        "ranked_charity_candidates": ranked_charity_pairs,
        "charity_registries": charity_registries,
        "qa_summary": qa_summary,
        "input_qa": input_qa_summary,
        "reference_check": reference_comparison,
        "output_workbook": config.workbook_file,
        "all_candidates_file": config.all_candidates_file,
        "all_charity_candidates_file": config.all_charity_candidates_file,
    }


# %% [markdown]
# ## 11. Lightweight preprocessing checks
#
# These run immediately and catch the specific V1 regressions before the
# 5.7-million-row scan starts.

# %%
def run_preprocessing_checks() -> None:
    assert normalise_postcode("RM12 3YT") == "RM123YT"
    assert postcode_sector("RM12 3YT") == "RM123"
    assert loose_name("ABC Services Limited") == "ABC SERVICES"
    assert loose_name("RIVIERA ELECTRICAL LIMTED") == "RIVIERA ELECTRICAL"
    aliases = split_name_aliases("ECSG LTD T/A Spectrum Electrical Group")
    assert ("REGISTERED_NAME", "ECSG LTD") in aliases
    assert ("TRADING_NAME", "Spectrum Electrical Group") in aliases
    assert is_valid_uk_postcode("RM12 3YT")
    assert not is_valid_uk_postcode("75016")
    assert classify_entity("STOCKPORT MBC", "SK1 3XE") == "PUBLIC_BODY_LIKELY"
    assert classify_entity("Example Electrical Ltd", "RM12 3YT") == "COMPANY_LIKELY"
    assert classify_entity("P Bradshaw", "NE1 1AA") == "SOLE_TRADER_OR_PERSON_LIKELY"
    assert classify_entity("OECD RUE ANDRE PASCAL 2", "75016") == "INTERNATIONAL_ENTITY_LIKELY"
    assert classify_entity("Example Foundation", "SW1A 1AA") == "CHARITY_LIKELY"
    assert ("LEGAL_CORE", "HSBC BANK PLC") in split_name_aliases("HSBC BANK PLC LONDON")
    assert prefix_containment_similarity(
        "CEC Electrical and Building Mainten",
        "CEC Electrical and Building Maintenance Limited",
    ) == 1.0
    assert normalise_company_number("123456") == "00123456"
    assert normalise_company_number("SC123456") == "SC123456"
    assert extract_uk_postcode("44 Alliance Avenue, Belfast, BT14 7PJ") == "BT147PJ"
    assert postcode_jurisdiction("BT14 7PJ") == "NORTHERN_IRELAND"
    assert postcode_jurisdiction("EH1 1AA") == "SCOTLAND"
    assert postcode_jurisdiction("SW1P 1PR") == "ENGLAND_WALES"
    LOGGER.info("Preprocessing checks passed")


run_preprocessing_checks()


# %% [markdown]
# ## 12. Run the production pipeline
#
# Confirm the input paths in `CONFIG`, then use **Run All**. V2.3 performs
# the input-alignment preflight before the expensive scan. Later runs reuse the cache only when the
# Companies House file and recipient blocking signature are unchanged.

# %%
if __name__ == "__main__":
    results = run_pipeline(CONFIG)
    display(results["qa_summary"])
