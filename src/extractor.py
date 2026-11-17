"""
Extract taxpayer and notice details from a notice's OCR result.

Finds:
  - taxpayer name(s) from the mailing address block (spouse, business,
    and care-of lines are recognised)
  - SSN / EIN / ITIN (stored masked: last 4 digits only)
  - notice or letter number (CP14, LTR1058, FTB4502, ...)
  - tax year(s) and tax period end date
  - notice date
  - issuing agency

Rule-based, built around how IRS (and some state) notices are laid
out. It never guesses silently: anything required it cannot find is
listed in "missing", and the notice is flagged for the exceptions
report.

Works from the OCR's visual lines and their positions. A label is
paired with the value on the same row to its right, or with the next
line, because OCR sometimes lists a value before its label.

Re-running costs nothing (no Azure call), so after improving a rule:
    python src/extractor.py <split batch folder>     # re-extract a batch
    python src/extractor.py <notice_001.ocr.json>    # show one notice
"""

import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from pdf_splitter import MANIFEST_NAME

logger = logging.getLogger(__name__)

# a notice is identified when all of these are found.
REQUIRED = ("client", "notice_number", "tax_period")


# --- lines ----------------------------------------------------------

@dataclass
class Line:
    index: int
    page: int
    text: str
    x0: float = None
    y0: float = None
    x1: float = None
    y1: float = None

    @property
    def has_box(self):
        return self.y0 is not None


def lines_from_ocr(raw):
    """Flatten Azure pages[].lines[] into Line objects in reading order."""
    lines = []

    for page in raw.get("pages") or []:
        for item in page.get("lines") or []:
            text = " ".join((item.get("content") or "").split())
            if not text:
                continue

            polygon = item.get("polygon") or []
            box = {}
            if len(polygon) >= 8:
                xs, ys = polygon[0::2], polygon[1::2]
                box = dict(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))

            lines.append(Line(len(lines), page.get("pageNumber", 1), text, **box))

    return lines


def _same_row_right(line, lines):
    """Lines on the same page and row, to the right of `line`, nearest first."""
    if not line.has_box:
        return []

    height = line.y1 - line.y0
    row = []

    for other in lines:
        if other is line or other.page != line.page or not other.has_box:
            continue
        overlap = min(line.y1, other.y1) - max(line.y0, other.y0)
        if overlap >= 0.5 * min(height, other.y1 - other.y0) and other.x0 > line.x0:
            row.append(other)

    return sorted(row, key=lambda other: other.x0)


def _value_candidates(line, label_end, lines):
    """
    Where a label's value may be: the rest of the same line, the next
    item on the same row, or the next line. If the rest of the line is
    some other text, the label belongs to something else; return [].
    """
    rest = line.text[label_end:].strip(" :#-\t")
    if rest:
        return [(rest, line)]

    candidates = [(other.text, other) for other in _same_row_right(line, lines)]
    if line.index + 1 < len(lines):
        following = lines[line.index + 1]
        candidates.append((following.text, following))

    return candidates


def find_labeled(lines, label_re, parse):
    """
    Return (value, evidence_text) for the first label whose value parses.
    `parse(text)` returns a value from the start of text, or None.
    """
    for line in lines:
        match = label_re.search(line.text)
        if not match:
            continue

        for text, source in _value_candidates(line, match.end(), lines):
            value = parse(text)
            if value is not None:
                evidence = line.text if source is line else f"{line.text} | {source.text}"
                return value, mask_tins(evidence), match

    return None, None, None


# --- taxpayer identification numbers ----------------------------------

_DASH = r"\s?-\s?"
SSN_RE = re.compile(
    rf"(?<![\w-])(?:\d{{3}}|[X*]{{3}}){_DASH}(?:\d{{2}}|[X*]{{2}}){_DASH}(\d{{4}})(?![\w-])",
    re.I,
)
EIN_RE = re.compile(
    rf"(?<![\w-])(?:\d{{2}}|[X*]{{2}}){_DASH}(?:\d{{3}}|[X*]{{3}})(\d{{4}})(?![\w-])",
    re.I,
)
PLAIN_NINE_RE = re.compile(r"^(\d{9})(?!\d)")

TIN_LABELS = [
    ("SSN", r"social\s+security\s+(?:number|no\b\.?)|\bSSN\b"),
    ("EIN", r"employer\s+(?:identification|id)\s+(?:number|no\b\.?)|\bEIN\b|\bFEIN\b"),
    ("ITIN", r"\bITIN\b|individual\s+taxpayer\s+identification"),
    ("TIN", r"taxpayer\s+(?:identification|id)\s+(?:number|no\b\.?)|\bTIN\b|identifying\s+number"),
]
TIN_LABEL_RE = re.compile("|".join(f"(?:{p})" for _, p in TIN_LABELS), re.I)
SPOUSE_RE = re.compile(r"spouse", re.I)


def mask_tins(text):
    """Replace any SSN/EIN in text with its masked form."""
    text = SSN_RE.sub(lambda m: f"XXX-XX-{m[1]}", text)
    return EIN_RE.sub(lambda m: f"XX-XXX{m[1]}", text)


def _tin_type_from_label(label):
    for tin_type, pattern in TIN_LABELS:
        if re.search(pattern, label, re.I):
            return tin_type
    return "TIN"


def _parse_tin(text):
    """(format, last4) from the start of text, or None."""
    text = text.strip(" :#")
    for kind, regex in (("SSN", SSN_RE), ("EIN", EIN_RE)):
        match = regex.match(text)
        if match:
            return kind, match[1]
    match = PLAIN_NINE_RE.match(text)
    if match:
        return "SSN", match[1][-4:]
    return None


def _masked(tin_type, last4):
    return f"XX-XXX{last4}" if tin_type == "EIN" else f"XXX-XX-{last4}"


def find_tins(lines):
    """All TINs on the notice, labeled ones first, de-duplicated."""
    found = []

    for line in lines:
        for match in TIN_LABEL_RE.finditer(line.text):
            for text, _source in _value_candidates(line, match.end(), lines):
                parsed = _parse_tin(text)
                if parsed:
                    fmt, last4 = parsed
                    tin_type = _tin_type_from_label(match[0])
                    if tin_type == "TIN":
                        tin_type = fmt
                    found.append(
                        {
                            "type": tin_type,
                            "last4": last4,
                            "spouse": bool(SPOUSE_RE.search(line.text)),
                            "labeled": True,
                        }
                    )
                    break

    # unlabeled, e.g. "XXX-XX-1234" printed on its own
    for line in lines:
        for kind, regex in (("SSN", SSN_RE), ("EIN", EIN_RE)):
            for match in regex.finditer(line.text):
                found.append(
                    {"type": kind, "last4": match[1], "spouse": False, "labeled": False}
                )

    unique = []
    seen = set()
    for tin in found:
        key = (tin["type"], tin["last4"])
        if key not in seen:
            seen.add(key)
            unique.append(tin)

    return unique


# --- notice / letter number -------------------------------------------

SELF_IDENTIFYING = [
    # (agency, regex, normaliser)
    ("IRS", re.compile(r"\bCP\s?-?(\d{2,4})(?:\s?([A-Z]))?\b"), lambda m: f"CP{m[1]}{m[2] or ''}"),
    (
        "IRS",
        re.compile(r"\b(?:Letter|LTR|Ltr)\s?-?(\d{3,4})(?:\s?([A-Z]{1,2}))?\b"),
        lambda m: f"LTR{m[1]}{m[2] or ''}",
    ),
    ("CA FTB", re.compile(r"\bFTB\s?-?(\d{3,4})(?:\s?([A-Z]{1,3}))?\b"), lambda m: f"FTB{m[1]}{m[2] or ''}"),
    ("NY DTF", re.compile(r"\bDTF\s?-\s?(\d{2,4})\b"), lambda m: f"DTF-{m[1]}"),
]

NOTICE_LABEL_RE = re.compile(
    r"^\s*(?:number\s+of\s+this\s+(?:notice|letter)|(?:notice|letter)"
    r"(?:\s+(?:number|no\b\.?|id|#))?)\s*:?",
    re.I,
)
GENERIC_NOTICE_RE = re.compile(r"^([A-Z]{0,4}[\s-]?\d{2,10}[A-Z]{0,3})(?=\s|$|\()")


def _normalise_notice(text, label=""):
    for agency, regex, normalise in SELF_IDENTIFYING:
        match = regex.match(text)
        if match:
            return normalise(match), agency
    match = GENERIC_NOTICE_RE.match(text)
    if match:
        number = re.sub(r"\s", "", match[1])
        # "Letter: 1058" is IRS Letter 1058
        if label.strip().lower().startswith("letter") and number[:1].isdigit():
            return f"LTR{number}", "IRS"
        return number, None
    return None


def _starts_self_identifying(text):
    return any(regex.match(text.strip()) for _, regex, _ in SELF_IDENTIFYING)


def find_notice_number(lines):
    """
    Return (number, agency, other_numbers, evidence).

    Scores every candidate: labeled ("Notice: CP14") and footer
    ("Notice CP14 (Rev. 1-2024)") occurrences count extra.
    """
    scores = {}
    agencies = {}
    evidence = {}
    first_seen = {}

    def add(number, agency, points, text, order):
        scores[number] = scores.get(number, 0) + points
        agencies.setdefault(number, agency)
        evidence.setdefault(number, text)
        first_seen.setdefault(number, order)

    for line in lines:
        # "Letter 1058 (Rev...)" is handled as a code below, not a label
        label = None if _starts_self_identifying(line.text) else NOTICE_LABEL_RE.match(line.text)
        if label:
            for text, source in _value_candidates(line, label.end(), lines):
                parsed = _normalise_notice(text, label[0])
                if parsed:
                    add(parsed[0], parsed[1], 3, f"{line.text} | {source.text}", line.index)
                    break

        for agency, regex, normalise in SELF_IDENTIFYING:
            for match in regex.finditer(line.text):
                footer = "(Rev" in line.text[match.end() : match.end() + 8]
                add(normalise(match), agency, 3 if footer else 1, line.text, line.index)

    if not scores:
        return None, None, [], None

    best = max(scores, key=lambda n: (scores[n], -first_seen[n]))
    others = sorted(n for n in scores if n != best)
    return best, agencies[best], others, evidence[best]


# --- dates and tax periods ----------------------------------------------

MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
DATE_RE = re.compile(
    rf"(?:(?:{MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}"
    r"|\d{1,2}[/-]\d{1,2}[/-](?:\d{4}|\d{2})"
    r"|\d{4}-\d{2}-\d{2})",
    re.I,
)
DATE_FORMATS = ("%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y", "%Y-%m-%d")


def parse_date(text):
    """Parse one date string; None if it is not a real date."""
    cleaned = re.sub(r"[,.]", "", text.strip())
    cleaned = re.sub(r"(?i)^sept\b", "Sep", cleaned)
    cleaned = " ".join(cleaned.split())

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _date_at_start(text):
    match = DATE_RE.match(text.strip(" :"))
    return parse_date(match[0]) if match else None


def _plausible_year(year):
    return 1980 <= year <= date.today().year + 1


PERIOD_LABEL_RE = re.compile(
    r"\b(?:tax(?:able)?\s+(?:years?|periods?)(?:\s+(?:ended|ending|end))?"
    r"|(?:period|year|quarter)\s+(?:ended|ending)"
    r"|for\s+(?:the\s+)?tax\s+(?:year|period))\b",
    re.I,
)
YEAR_LIST_RE = re.compile(r"^((?:19|20)\d{2})((?:\s*(?:,|and|&|-|through|thru)\s*(?:19|20)\d{2})*)(?!\d)")
YYYYMM_RE = re.compile(r"^((?:19|20)\d{2})(0[1-9]|1[0-2])(?!\d)")
RANGE_SEP_RE = re.compile(r"^\s*(?:-|to|through|thru)\s*", re.I)


def _parse_period(text):
    """(years, period_end) from the start of text, or None."""
    text = text.strip(" :")

    start = DATE_RE.match(text)
    if start:
        end_date = parse_date(start[0])
        rest = text[start.end() :]
        separator = RANGE_SEP_RE.match(rest)
        if separator:
            second = DATE_RE.match(rest[separator.end() :])
            if second and parse_date(second[0]):
                end_date = parse_date(second[0])
        if end_date and _plausible_year(end_date.year):
            return [end_date.year], end_date
        return None

    yyyymm = YYYYMM_RE.match(text)
    if yyyymm:
        year, month = int(yyyymm[1]), int(yyyymm[2])
        if _plausible_year(year):
            next_month = date(year + month // 12, month % 12 + 1, 1)
            return [year], next_month - timedelta(days=1)

    years = YEAR_LIST_RE.match(text)
    if years:
        values = sorted({int(y) for y in re.findall(r"(?:19|20)\d{2}", years[0])})
        if all(_plausible_year(y) for y in values):
            return values, None

    return None


def find_tax_period(lines):
    """(years, period_end, evidence) from all labeled occurrences."""
    years = set()
    period_end = None
    evidence = None

    for line in lines:
        for match in PERIOD_LABEL_RE.finditer(line.text):
            for text, source in _value_candidates(line, match.end(), lines):
                parsed = _parse_period(text)
                if parsed:
                    found_years, found_end = parsed
                    years.update(found_years)
                    if period_end is None and found_end:
                        period_end = found_end
                    if evidence is None:
                        evidence = line.text if source is line else f"{line.text} | {source.text}"
                    break

    return sorted(years), period_end, evidence


NOTICE_DATE_LABEL_RE = re.compile(
    r"\b(?:notice\s+date|date\s+of\s+(?:this\s+)?(?:notice|letter)|letter\s+date"
    r"|date\s+issued|issue\s+date|date\s+mailed|mailing\s+date)\b|^\s*date\b",
    re.I,
)


def find_notice_date(lines):
    today = date.today()

    def parse(text):
        value = _date_at_start(text)
        if value and date(2000, 1, 1) <= value <= today + timedelta(days=60):
            return value
        return None

    value, evidence, _ = find_labeled(lines, NOTICE_DATE_LABEL_RE, parse)
    return value, evidence


# --- agency -------------------------------------------------------------

AGENCIES = [
    ("IRS", re.compile(r"internal\s+revenue\s+service|department\s+of\s+the\s+treasury", re.I)),
    ("CA FTB", re.compile(r"franchise\s+tax\s+board", re.I)),
    ("NY DTF", re.compile(r"department\s+of\s+taxation\s+and\s+finance", re.I)),
    ("State", re.compile(r"department\s+of\s+(?:revenue|taxation)|comptroller|tax\s+commission", re.I)),
]


def find_agency(lines):
    for line in lines:
        for agency, regex in AGENCIES:
            if regex.search(line.text):
                return agency
    return None


# --- taxpayer name from the address block --------------------------------

CITY_STATE_ZIP_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]*,?\s+[A-Z]{2}\.?\s+\d{5}(?:-\d{4})?$")
STREET_RE = re.compile(
    r"^(?:\d+[A-Za-z]?\s+\S|P\.?\s?O\.?\s*BOX\b|BOX\s+\d|APT\b|SUITE\b|STE\b|UNIT\b|RR\s+\d|HC\s+\d|#\s?\d)",
    re.I,
)
NAME_LINE_RE = re.compile(r"^[A-Za-z%][A-Za-z .,'&%/-]*$")
AGENCY_WORDS_RE = re.compile(
    r"internal\s+revenue|department\s+of|treasury|franchise\s+tax|taxation|comptroller"
    r"|revenue\s+service|state\s+of\b|\bIRS\b",
    re.I,
)
CARE_OF_RE = re.compile(r"^(?:%|C/O\b|ATTN:?\b|IN\s+CARE\s+OF\b)\s*", re.I)
BUSINESS_RE = re.compile(
    r"\b(?:LLC|L\.L\.C|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LLP|LP|LTD|PC|PA|PLLC"
    r"|TRUST|ESTATE|FOUNDATION|ASSOCIATION|ASSN|PARTNERSHIP|PARTNERS|GROUP|HOLDINGS"
    r"|ENTERPRISES|SERVICES|DBA|CHURCH|MINISTRIES|CLUB|FUND)\b",
    re.I,
)
INITIAL_RE = re.compile(r"^[A-Z]\.?$", re.I)


def _mostly_upper(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) >= 0.8


def _aligned(a, b):
    """Same column (when positions are known)."""
    if not (a.has_box and b.has_box):
        return True
    return a.page == b.page and abs(a.x0 - b.x0) < 0.5


def address_blocks(lines):
    """Yield (name_lines, evidence) for each taxpayer-looking address block."""
    for city in lines:
        if not CITY_STATE_ZIP_RE.match(city.text):
            continue

        j = city.index - 1
        streets = []
        while j >= 0 and len(streets) < 2 and STREET_RE.match(lines[j].text) and _aligned(lines[j], city):
            streets.insert(0, lines[j])
            j -= 1

        if not streets:
            continue

        names = []
        while j >= 0 and len(names) < 3:
            candidate = lines[j]
            text = candidate.text
            if (
                not NAME_LINE_RE.match(text)
                or len(text) > 60
                or not _aligned(candidate, city)
                # mixed-case lines ("Amount due") are only accepted
                # directly above the street line
                or (names and not _mostly_upper(text))
            ):
                break
            names.insert(0, candidate)
            j -= 1

        block = [*names, *streets, city]
        if not names or any(AGENCY_WORDS_RE.search(line.text) for line in block):
            continue

        yield [line.text for line in names], " / ".join(line.text for line in block)


def _split_joint(line):
    """'JOHN A & JANE B SMITH' -> ('JOHN A SMITH', 'JANE B SMITH')."""
    parts = re.split(r"\s+(?:&|AND)\s+", line, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return line, None

    left, right = parts
    left_tokens = left.split()
    right_tokens = right.split()

    if len(right_tokens) >= 2 and (len(left_tokens) == 1 or INITIAL_RE.match(left_tokens[-1])):
        left = f"{left} {right_tokens[-1]}"

    return left, right


def parse_names(name_lines):
    """Return dict(entity_type, taxpayer_name, spouse_name, care_of)."""
    care_of = None
    names = []

    for line in name_lines:
        if CARE_OF_RE.match(line):
            care_of = care_of or CARE_OF_RE.sub("", line).strip()
        else:
            names.append(line.strip(" ,"))

    result = {"entity_type": None, "taxpayer_name": None, "spouse_name": None, "care_of": care_of}

    if not names:
        return result

    if any(BUSINESS_RE.search(name) for name in names):
        result.update(entity_type="business", taxpayer_name=" ".join(names))
        return result

    primary, spouse = _split_joint(names[0])
    if spouse is None and len(names) > 1:
        spouse = names[1]

    result.update(entity_type="individual", taxpayer_name=primary, spouse_name=spouse)
    return result


NAME_LABEL_RE = re.compile(r"^\s*(?:taxpayer(?:'s)?\s+name|name\s+of\s+taxpayer|taxpayer|name)\s*:", re.I)


def find_taxpayer(lines):
    """Names from the first taxpayer address block (or a 'Name:' label)."""
    blocks = list(address_blocks(lines))

    if blocks:
        name_lines, evidence = blocks[0]
        distinct = {tuple(block[0]) for block in blocks}
        return parse_names(name_lines), evidence, len(distinct)

    def parse(text):
        text = text.strip()
        return text if NAME_LINE_RE.match(text) and len(text) <= 60 else None

    value, evidence, _ = find_labeled(lines, NAME_LABEL_RE, parse)
    if value:
        return parse_names([value]), evidence, 1

    return parse_names([]), None, 0


# --- notice -------------------------------------------------------------

def extract(raw):
    """Extract everything from one Azure OCR result (the "raw" dict)."""
    lines = lines_from_ocr(raw)
    evidence = {}
    warnings = []

    taxpayer, evidence["taxpayer"], name_blocks = find_taxpayer(lines)
    if name_blocks > 1:
        warnings.append(
            "more than one taxpayer address found - two notices merged (missing separator?)"
        )

    tins = find_tins(lines)
    primary_tins = [t for t in tins if not t["spouse"]]
    spouse_tins = [t for t in tins if t["spouse"]]
    tin = next((t for t in primary_tins if t["labeled"]), primary_tins[0] if primary_tins else None)

    if len({t["last4"] for t in primary_tins if t["labeled"]}) > 1:
        warnings.append("more than one taxpayer ID number found - check the client")
    if tin and not tin["labeled"]:
        warnings.append("taxpayer ID number found without a label - verify it")

    number, number_agency, other_numbers, evidence["notice_number"] = find_notice_number(lines)
    if other_numbers:
        warnings.append(f"other notice/letter numbers also mentioned: {', '.join(other_numbers)}")

    years, period_end, evidence["tax_period"] = find_tax_period(lines)
    notice_date, evidence["notice_date"] = find_notice_date(lines)

    result = {
        "agency": find_agency(lines) or number_agency,
        "notice_number": number,
        "tax_years": years,
        "tax_period_end": period_end.isoformat() if period_end else None,
        "notice_date": notice_date.isoformat() if notice_date else None,
        **taxpayer,
        "tin_type": tin["type"] if tin else None,
        "tin_masked": _masked(tin["type"], tin["last4"]) if tin else None,
        "tin_last4": tin["last4"] if tin else None,
        "spouse_tin_last4": spouse_tins[0]["last4"] if spouse_tins else None,
    }

    found = {
        "client": bool(result["taxpayer_name"] or result["tin_last4"]),
        "notice_number": bool(number),
        "tax_period": bool(years or period_end),
    }
    missing = [field for field in REQUIRED if not found[field]]

    if not result["notice_date"]:
        warnings.append("notice date not found")
    if found["client"] and not result["taxpayer_name"]:
        warnings.append("taxpayer name not found (ID number only)")
    if found["client"] and not result["tin_last4"]:
        warnings.append("taxpayer ID number not found (name only)")

    result.update(
        status="identified" if not missing else "incomplete",
        missing=missing,
        warnings=warnings,
        evidence={key: value for key, value in evidence.items() if value},
    )
    return result


def problems(extracted):
    """Human-readable reasons a notice needs review."""
    return [f"could not find {field.replace('_', ' ')}" for field in extracted["missing"]] + [
        w for w in extracted["warnings"] if w != "notice date not found"
    ]


# --- batch --------------------------------------------------------------

def extract_batch(batch_dir):
    """
    Extract every OCR'd notice in a split batch into its manifest.

    Safe to re-run: each notice's "extracted" entry is replaced.
    """
    from ocr import ocr_path  # local import: keeps this module Azure-free

    batch_dir = Path(batch_dir)
    manifest_path = batch_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    counts = {"identified": 0, "incomplete": 0}

    for notice in manifest["notices"]:
        ocr_file = ocr_path(batch_dir / notice["file"])

        if ocr_file.is_file():
            record = json.loads(ocr_file.read_text(encoding="utf-8"))
            extracted = extract(record["raw"])
        else:
            extracted = {
                "status": "incomplete",
                "missing": list(REQUIRED),
                "warnings": ["no OCR result"],
            }

        notice["extracted"] = extracted
        notice["needs_review"] = bool(notice["warnings"]) or bool(problems(extracted))
        counts[extracted["status"]] += 1

    partial = manifest_path.with_name(manifest_path.name + ".partial")
    partial.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    partial.replace(manifest_path)

    logger.info(
        "Extracted %s: %s identified, %s incomplete",
        batch_dir.name,
        counts["identified"],
        counts["incomplete"],
    )
    return manifest


def _print_notice(name, extracted):
    print(f"== {name}: {extracted['status'].upper()}")
    for key in (
        "agency", "notice_number", "tax_years", "tax_period_end", "notice_date",
        "entity_type", "taxpayer_name", "spouse_name", "care_of", "tin_masked",
        "spouse_tin_last4",
    ):
        if extracted.get(key):
            print(f"   {key:16} {extracted[key]}")
    for reason in problems(extracted):
        print(f"   REVIEW: {reason}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) != 2:
        sys.exit("usage: python src/extractor.py <batch folder | notice.ocr.json>")

    target = Path(sys.argv[1])

    if target.is_dir():
        for item in extract_batch(target)["notices"]:
            _print_notice(item["file"], item["extracted"])
    else:
        _print_notice(target.name, extract(json.loads(target.read_text(encoding="utf-8"))["raw"]))
