import json

import pytest

import config
import intake_watcher
from extractor import extract, extract_batch, parse_names
from generate_test_batch import build_batch
from ocr import ocr_batch
from pdf_splitter import batch_dir, split_pdf
from processing_history import initialize_database


# --- helpers ------------------------------------------------------------

def raw_from(*lines):
    """
    Build an Azure-shaped result. Each line is either "text" (no
    position) or ("text", x_inches, y_inches) on page 1.
    """
    items = []
    for line in lines:
        if isinstance(line, str):
            items.append({"content": line})
        else:
            text, x, y = line
            x1 = x + 0.08 * len(text)
            items.append({"content": text, "polygon": [x, y, x1, y, x1, y + 0.15, x, y + 0.15]})

    return {"content": "\n".join(i["content"] for i in items), "pages": [{"pageNumber": 1, "lines": items}]}


ADDRESS = ["JOHN SMITH", "123 MAIN ST", "ANYTOWN, TX 75001-1234"]


# --- realistic notices end to end ------------------------------------------

VARIETY_EXPECTED = [
    dict(status="identified", agency="IRS", notice_number="CP14", tax_years=[2023],
         notice_date="2024-05-20", entity_type="individual", taxpayer_name="JOHN A SMITH",
         spouse_name="JANE B SMITH", tin_type="SSN", tin_last4="1234"),
    dict(status="identified", agency="IRS", notice_number="CP2000", tax_years=[2022],
         notice_date="2024-08-05", taxpayer_name="MARIA GARCIA", spouse_name=None,
         tin_last4="5678"),
    dict(status="identified", agency="IRS", notice_number="LTR1058", tax_years=[2021],
         tax_period_end="2021-12-31", notice_date="2024-03-11", taxpayer_name="ROBERT JONES",
         spouse_name="LINDA JONES", tin_type="SSN", tin_last4="4321"),
    dict(status="identified", agency="IRS", notice_number="CP161", tax_years=[2024],
         tax_period_end="2024-03-31", notice_date="2024-06-03", entity_type="business",
         taxpayer_name="ACME WIDGETS LLC", care_of="PAT LEE MBR", tin_type="EIN",
         tin_masked="XX-XXX6789"),
    # EIN assignment letters have no tax year: correctly an exception
    dict(status="incomplete", missing=["tax_period"], notice_number="CP575A",
         notice_date="2024-03-15", entity_type="business",
         taxpayer_name="BRIGHT FUTURES FOUNDATION", tin_type="EIN", tin_masked="XX-XXX6789"),
    dict(status="identified", agency="CA FTB", notice_number="FTB4502", tax_years=[2022],
         notice_date="2024-04-22", taxpayer_name="DANA WHITE", tin_last4="2468"),
    dict(status="incomplete", missing=["client", "notice_number", "tax_period"]),
]


@pytest.fixture
def variety_batch():
    path = config.INTAKE_DIR / "variety.pdf"
    with build_batch("batch_17_notice_variety") as doc:
        doc.save(path)
    folder = batch_dir(split_pdf(path))
    ocr_batch(folder)
    return folder


def test_realistic_notices_are_extracted(variety_batch):
    notices = extract_batch(variety_batch)["notices"]

    assert len(notices) == len(VARIETY_EXPECTED)
    for number, (notice, expected) in enumerate(zip(notices, VARIETY_EXPECTED), start=1):
        got = notice["extracted"]
        for key, value in expected.items():
            assert got.get(key) == value, f"notice {number} {key}: {got}"


def test_review_flags_follow_extraction(variety_batch):
    notices = extract_batch(variety_batch)["notices"]

    assert [n["needs_review"] for n in notices] == [False] * 4 + [True, False, True]


def test_extraction_is_rerunnable(variety_batch):
    first = extract_batch(variety_batch)
    second = extract_batch(variety_batch)

    assert [n["extracted"] for n in first["notices"]] == [n["extracted"] for n in second["notices"]]


def test_no_full_ssn_is_stored(variety_batch, tmp_path):
    # a notice that prints the full SSN
    raw = raw_from("Social Security number", "123-45-6789", *ADDRESS, "Notice CP14", "Tax year 2023")
    result = extract(raw)

    assert result["tin_masked"] == "XXX-XX-6789"
    assert "123-45" not in json.dumps(result)


def test_watcher_extracts_after_ocr(monkeypatch):
    initialize_database()
    monkeypatch.setattr(intake_watcher, "STABLE_SECONDS", 0)
    monkeypatch.setattr(intake_watcher, "sleep", lambda _seconds: None)
    monkeypatch.setattr(intake_watcher, "_paused_until", 0.0)

    path = config.INTAKE_DIR / "variety.pdf"
    with build_batch("batch_17_notice_variety") as doc:
        doc.save(path)

    intake_watcher.process_file(path)

    manifest = json.loads(next(config.SPLIT_DIR.glob("*/manifest.json")).read_text())
    assert [n["extracted"]["status"] for n in manifest["notices"]] == [
        "identified", "identified", "identified", "identified",
        "incomplete", "identified", "incomplete",
    ]


# --- layout and OCR quirks ------------------------------------------------

def test_value_listed_before_its_label_on_the_same_row():
    # real Azure output for Letter 1058: value line came first
    raw = raw_from(
        ("XXX-XX-4321", 6.5, 0.88),
        ("Taxpayer ID number", 4.7, 0.90),
        ("December 31, 2021", 6.5, 1.08),
        ("Tax periods", 4.7, 1.10),
        ("Letter 1058 (Rev. 7-2022)", 1.0, 10.3),
    )
    result = extract(raw)

    assert result["tin_last4"] == "4321"
    assert result["tax_period_end"] == "2021-12-31"
    assert result["notice_number"] == "LTR1058"
    assert result["warnings"] == ["notice date not found", "taxpayer name not found (ID number only)"]


@pytest.mark.parametrize(
    "lines, years, period_end",
    [
        (["Tax year: 2023"], [2023], None),
        (["Tax year", "2023"], [2023], None),
        (["Taxable Year:", "2022"], [2022], None),
        (["Tax period: 202312"], [2023], "2023-12-31"),
        (["Tax period: 01/01/2022 - 12/31/2022"], [2022], "2022-12-31"),
        (["Tax years 2020, 2021 and 2022"], [2020, 2021, 2022], None),
        (["Tax year ended December 31, 2023"], [2023], "2023-12-31"),
        (["Quarter ending: June 30, 2024"], [2024], "2024-06-30"),
        (["This is about your tax year 2019 return"], [2019], None),
        (["Tax year: 1820"], [], None),  # implausible
    ],
)
def test_tax_period_formats(lines, years, period_end):
    result = extract(raw_from(*lines))

    assert result["tax_years"] == years
    assert result["tax_period_end"] == period_end


@pytest.mark.parametrize(
    "line, number",
    [
        ("Notice CP14", "CP14"),
        ("CP 575 A", "CP575A"),
        ("Letter 5071C", "LTR5071C"),
        ("LTR 3172", "LTR3172"),
        ("Notice CP2000 (Rev. 3-2023)", "CP2000"),
        ("DTF-160", "DTF-160"),
        ("Letter number: 0012", "LTR0012"),
        ("Notice number: L1234567890", "L1234567890"),
    ],
)
def test_notice_number_formats(line, number):
    assert extract(raw_from(line))["notice_number"] == number


def test_due_dates_are_not_notice_dates():
    raw = raw_from("Pay by June 10, 2024", "Due date: June 10, 2024", "Respond by July 1, 2024")

    assert extract(raw)["notice_date"] is None


@pytest.mark.parametrize(
    "lines, date",
    [
        (["Notice date", "May 20, 2024"], "2024-05-20"),
        (["Date of this notice: 03-15-2024"], "2024-03-15"),
        (["Letter date: 3/11/24"], "2024-03-11"),
        (["Date: Sept. 9, 2024"], "2024-09-09"),
    ],
)
def test_notice_date_formats(lines, date):
    assert extract(raw_from(*lines))["notice_date"] == date


def test_numbers_that_are_not_tins():
    raw = raw_from(
        *ADDRESS,  # ZIP+4 75001-1234
        "To contact us 800-829-0922",
        "AUR control number 1234567890",
        "Your caller ID 482913",
    )
    assert extract(raw)["tin_last4"] is None


def test_ocr_variants_of_masked_ssn():
    assert extract(raw_from("SSN: xxx-xx- 9876"))["tin_last4"] == "9876"
    assert extract(raw_from("Taxpayer ID number: ***-**-5555"))["tin_last4"] == "5555"


def test_spouse_ssn_is_separate():
    raw = raw_from("Primary SSN: XXX-XX-1111", "Spouse's SSN: XXX-XX-2222")
    result = extract(raw)

    assert result["tin_last4"] == "1111"
    assert result["spouse_tin_last4"] == "2222"


def test_unlabeled_ssn_is_used_but_flagged():
    result = extract(raw_from("XXX-XX-3030"))

    assert result["tin_last4"] == "3030"
    assert any("without a label" in w for w in result["warnings"])


def test_agency_addresses_are_not_taxpayers():
    raw = raw_from(
        "Department of the Treasury", "Internal Revenue Service", "Ogden, UT 84201-0039",
        "Internal Revenue Service", "P.O. Box 931000", "Louisville, KY 40293-1000",
        "STATE OF CALIFORNIA", "FRANCHISE TAX BOARD", "PO BOX 942867", "SACRAMENTO CA 94267-0001",
    )
    assert extract(raw)["taxpayer_name"] is None


def test_text_above_the_name_is_not_part_of_it():
    raw = raw_from("Amount due", *ADDRESS)
    result = extract(raw)

    assert result["taxpayer_name"] == "JOHN SMITH"
    assert result["spouse_name"] is None


def test_two_different_taxpayers_means_missing_separator():
    raw = raw_from(*ADDRESS, "Notice CP14", "MARY JONES", "9 ELM ST", "OTHERTOWN, OH 45402")
    result = extract(raw)

    assert result["taxpayer_name"] == "JOHN SMITH"
    assert any("missing separator" in w for w in result["warnings"])


def test_name_label_fallback():
    assert extract(raw_from("Taxpayer name: Pat Doe"))["taxpayer_name"] == "Pat Doe"


@pytest.mark.parametrize(
    "lines, expected",
    [
        (["JOHN & JANE SMITH"], ("individual", "JOHN SMITH", "JANE SMITH", None)),
        (["JOHN A & JANE B SMITH"], ("individual", "JOHN A SMITH", "JANE B SMITH", None)),
        (["JOHN SMITH AND JANE DOE"], ("individual", "JOHN SMITH", "JANE DOE", None)),
        (["ROBERT JONES", "LINDA JONES"], ("individual", "ROBERT JONES", "LINDA JONES", None)),
        (["MARIA GARCIA"], ("individual", "MARIA GARCIA", None, None)),
        (["ACME WIDGETS LLC", "% PAT LEE MBR"], ("business", "ACME WIDGETS LLC", None, "PAT LEE MBR")),
        (["SMITH FAMILY", "TRUST"], ("business", "SMITH FAMILY TRUST", None, None)),
        (["ESTATE OF JOHN SMITH"], ("business", "ESTATE OF JOHN SMITH", None, None)),
        (["JANE DOE", "C/O JOHN DOE"], ("individual", "JANE DOE", None, "JOHN DOE")),
    ],
)
def test_name_parsing(lines, expected):
    result = parse_names(lines)
    assert (
        result["entity_type"],
        result["taxpayer_name"],
        result["spouse_name"],
        result["care_of"],
    ) == expected
