"""
Generate synthetic test batches. FICTIONAL DATA ONLY.

Each batch declares what the splitter should produce.
tests/test_splitter_batches.py builds and checks every batch here,
so add a batch whenever a new real-world edge case turns up.

Usage:
    python scripts/generate_test_batch.py
    -> <BASE_DIR>/test-batches/qr-separators/*.pdf
"""

import io
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import qrcode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
from separator_sheet import LETTER, add_separator_page  # noqa: E402

SCAN_DPI = 150

FILLER = (
    "We changed your return to correct a miscalculation. As a result, "
    "you owe the balance shown above. Pay the amount due by the date "
    "shown to avoid additional penalty and interest charges."
)


# --- expectations -------------------------------------------------

@dataclass
class Expected:
    notice_pages: list  # page count of each notice; [] if rejected
    rejected: bool = False
    batch_warning: bool = False
    review_notices: tuple = ()  # 1-based notices expected to be flagged


@dataclass
class BatchSpec:
    build: callable
    expected: Expected
    duplex: bool = False
    scan: dict = None  # None = clean digital PDF
    upside_down: set = field(default_factory=set)  # 0-based pages


BATCHES = {}


def batch(name, notice_pages, *, duplex=False, scan=None, upside_down=(), **expected):
    def register(fn):
        BATCHES[name] = BatchSpec(
            build=fn,
            expected=Expected(notice_pages, **expected),
            duplex=duplex,
            scan=scan,
            upside_down=set(upside_down),
        )
        return fn

    return register


# --- page builders --------------------------------------------------

def _qr_png(text, box_size=6):
    buffer = io.BytesIO()
    qrcode.make(text, box_size=box_size, border=2).save(buffer, format="PNG")
    return buffer.getvalue()


class BatchBuilder:
    def __init__(self, duplex):
        self.doc = pymupdf.open()
        self.duplex = duplex

    def _new_page(self):
        return self.doc.new_page(width=LETTER.width, height=LETTER.height)

    def _back_side(self):
        # duplex scanning captures the blank back of one-sided pages.
        if self.duplex:
            self._new_page()

    def notice(
        self,
        client,
        tax_id,
        notice_type,
        pages=1,
        *,
        sparse=False,
        payment_qr=False,
        left_blank=(),
    ):
        for number in range(1, pages + 1):
            page = self._new_page()

            if number in left_blank:
                page.insert_text(
                    (190, 400), "This page intentionally left blank.", fontsize=12
                )

            elif sparse:
                page.insert_text((72, 100), f"Test notice for {client}", fontsize=10)

            else:
                page.insert_text(
                    (72, 90), "SYNTHETIC TAX NOTICE - TEST ONLY", fontsize=15
                )
                lines = [
                    f"Client: {client}",
                    f"Tax ID: {tax_id}",
                    f"Notice Type: {notice_type}",
                    f"Page {number} of {pages}",
                    "",
                ] + [FILLER[i : i + 80] for i in range(0, len(FILLER), 80)] * 5

                y = 130
                for line in lines:
                    page.insert_text((72, y), line, fontsize=11)
                    y += 18

            if payment_qr and number == 1:
                # notices often carry their own QR codes; these must
                # never be mistaken for separators.
                rect = pymupdf.Rect(460, 40, 540, 120)
                page.insert_image(
                    rect, stream=_qr_png("https://example.com/pay?notice=CP14")
                )

            self._back_side()

    def formal_notice(
        self,
        *,
        agency_lines,
        rows,
        name_lines,
        street="123 MAIN ST",
        city="ANYTOWN, TX 75001-1234",
        title="",
        body_lines=(),
        pages=1,
        footer="",
        payment_address=None,
    ):
        """
        A notice laid out like real IRS / state mail:
          - agency return address, top left
          - label / value block, top right (labels and values in
            separate columns, as on IRS notices)
          - the taxpayer's mailing block
          - title and body text
          - optional payment voucher on the last page, repeating the
            taxpayer block next to the agency's payment address
        """
        body = list(body_lines) + [
            FILLER[i : i + 80] for i in range(0, len(FILLER), 80)
        ] * 3

        for number in range(1, pages + 1):
            page = self._new_page()

            if number == 1:
                y = 60
                for line in agency_lines:
                    page.insert_text((72, y), line, fontsize=10)
                    y += 13

                y = 60
                for label, value in rows:
                    # keep a clear gap after long labels
                    label_end = 340 + pymupdf.get_text_length(label, fontsize=10)
                    page.insert_text((340, y), label, fontsize=10)
                    page.insert_text((max(470, label_end + 15), y), value, fontsize=10)
                    y += 15

                y = 190
                for line in [*name_lines, street, city]:
                    page.insert_text((90, y), line, fontsize=11)
                    y += 14

                y = 290
                if title:
                    page.insert_text((72, y), title, fontsize=14)
                    y += 28

            else:
                label, value = rows[0]
                page.insert_text((72, 60), f"{label} {value}", fontsize=10)
                page.insert_text((470, 60), f"Page {number} of {pages}", fontsize=10)
                y = 100

            for line in body:
                page.insert_text((72, y), line, fontsize=11)
                y += 16

            if number == pages and payment_address:
                y = 600
                page.insert_text((72, y), "Payment", fontsize=12)
                for label, value in rows[:1]:
                    page.insert_text((340, y), f"{label} {value}", fontsize=10)
                y += 30
                for line in [*name_lines, street, city]:
                    page.insert_text((72, y), line, fontsize=10)
                    y += 13
                y = 640
                for line in payment_address:
                    page.insert_text((340, y), line, fontsize=10)
                    y += 13

            if footer:
                page.insert_text((72, 750), footer, fontsize=8)

            self._back_side()

    def separator(self, damaged=False):
        page = add_separator_page(self.doc)

        if damaged:
            # sticky note / coffee stain over the top QR code
            page.draw_rect(
                pymupdf.Rect(220, 110, 400, 260), color=None, fill=(0.2, 0.2, 0.2)
            )

        self._back_side()

    def blank(self):
        self._new_page()


# --- scan simulation ------------------------------------------------

def simulate_scan(doc, seed=0, paper=232, skew=0.7, upside_down=()):
    """
    Turn a clean PDF into something that looks like an office scan:
    tinted paper, sensor noise, speckle, bleed-through, edge shadows,
    punch holes, skew, blur, and JPEG compression.
    """
    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)
    scanned = pymupdf.open()
    previous = None

    for index, page in enumerate(doc):
        pix = page.get_pixmap(colorspace=pymupdf.csGRAY, dpi=SCAN_DPI, alpha=False)
        original = np.frombuffer(pix.samples, np.uint8).reshape(
            pix.height, pix.stride
        )[:, : pix.width]

        img = original.astype(np.float32) / 255 * paper

        # bleed-through from the front of the sheet onto its blank back
        is_back_side = original.min() > 250
        if is_back_side and previous is not None and previous.shape == img.shape:
            img -= (255 - np.fliplr(previous).astype(np.float32)) * 0.12

        img += np_rng.normal(0, 3, img.shape)

        h, w = img.shape
        for _ in range(250):  # speckle
            img[np_rng.integers(0, h), np_rng.integers(0, w)] = rng.uniform(40, 200)

        shadow = np.linspace(90, 0, 14)
        img[:, :14] -= shadow  # left edge shadow
        img[:14, :] -= shadow[:, None]  # top edge shadow

        for y in (0.2, 0.5, 0.8):  # three-hole punch
            cv2.circle(img, (int(0.4 * SCAN_DPI), int(y * h)), 18, 30, -1)

        angle = rng.uniform(-skew, skew)
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        img = cv2.warpAffine(img, matrix, (w, h), borderValue=40)
        img = cv2.GaussianBlur(img, (3, 3), 0)

        if index in upside_down:
            img = np.rot90(img, 2)

        img = np.clip(img, 0, 255).astype(np.uint8)
        ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        assert ok

        out_page = scanned.new_page(width=LETTER.width, height=LETTER.height)
        out_page.insert_image(out_page.rect, stream=jpeg.tobytes())

        previous = original

    return scanned


def build_batch(name):
    """Return a PyMuPDF document for the named batch."""
    spec = BATCHES[name]
    builder = BatchBuilder(spec.duplex)
    spec.build(builder)

    if spec.scan is None:
        return builder.doc

    scanned = simulate_scan(builder.doc, upside_down=spec.upside_down, **spec.scan)
    builder.doc.close()
    return scanned


# --- batches --------------------------------------------------------

@batch("batch_01_standard", [2, 1, 1])
def _(b):
    b.notice("John Smith", "XXX-XX-1001", "IRS Balance Due", 2)
    b.separator()
    b.notice("Jane Doe", "XXX-XX-1002", "IRS Penalty Notice")
    b.separator()
    b.notice("ABC Company", "XX-XXX1003", "Payroll Tax Notice")


@batch("batch_02_single", [2], batch_warning=True)
def _(b):
    b.notice("Taylor Johnson", "XXX-XX-2001", "IRS Payment Reminder", 2)


@batch("batch_03_multipage", [3, 3])
def _(b):
    b.notice("Morgan Lee", "XXX-XX-3001", "IRS Examination Notice", 3)
    b.separator()
    b.notice("Jordan Davis", "XXX-XX-3002", "IRS Collection Notice", 3)


@batch("batch_04_large", [1, 2, 3, 1, 2])
def _(b):
    clients = [
        ("Alex Rivera", "XXX-XX-4001", 1),
        ("Sam Parker", "XXX-XX-4002", 2),
        ("Chris Taylor", "XXX-XX-4003", 3),
        ("Jamie Wilson", "XXX-XX-4004", 1),
        ("Casey Brown", "XXX-XX-4005", 2),
    ]
    for index, (name, tax_id, pages) in enumerate(clients):
        if index:
            b.separator()
        b.notice(name, tax_id, "IRS Tax Notice", pages)


@batch("batch_05_double_separator", [2, 1])
def _(b):
    b.notice("Robin Miller", "XXX-XX-5001", "IRS Notice", 2)
    b.separator()
    b.separator()
    b.notice("Drew Anderson", "XXX-XX-5002", "IRS Notice")


@batch("batch_06_sparse", [1, 1])
def _(b):
    b.notice("Avery Clark", "XXX-XX-6001", "Sparse", sparse=True)
    b.separator()
    b.notice("Riley Brooks", "XXX-XX-6002", "Sparse", sparse=True)


@batch("batch_07_blank_only", [], rejected=True)
def _(b):
    for _ in range(3):
        b.blank()


@batch("batch_08_duplex", [2, 1, 3], duplex=True)
def _(b):
    b.notice("Quinn Hayes", "XXX-XX-8001", "IRS Notice", 2)
    b.separator()
    b.notice("Parker Gray", "XXX-XX-8002", "State Notice")
    b.separator()
    b.notice("Hayden Cole", "XX-XXX8003", "Payroll Notice", 3)


@batch("batch_09_scanned_duplex", [2, 1, 2], duplex=True, scan={"seed": 9})
def _(b):
    b.notice("Emerson Fox", "XXX-XX-9001", "IRS Notice", 2)
    b.separator()
    b.notice("Rowan Price", "XXX-XX-9002", "IRS Notice")
    b.separator()
    b.notice("Sage Bennett", "XXX-XX-9003", "State Notice", 2)


@batch("batch_10_notice_with_own_qr", [2, 1])
def _(b):
    b.notice("Blake Ward", "XXX-XX-1101", "IRS CP14", 2, payment_qr=True)
    b.separator()
    b.notice("Reese Long", "XXX-XX-1102", "IRS CP14", payment_qr=True)


@batch("batch_11_separators_at_ends", [1, 1])
def _(b):
    b.separator()
    b.notice("Kendall Ross", "XXX-XX-1201", "IRS Notice")
    b.separator()
    b.notice("Finley Hart", "XXX-XX-1202", "IRS Notice")
    b.separator()


@batch("batch_12_missing_separator", [17, 1], review_notices=(1,))
def _(b):
    b.notice("Dakota Reed", "XXX-XX-1301", "IRS Exam Report", 9)
    # staff forgot a separator here
    b.notice("Harper Stone", "XXX-XX-1302", "IRS Exam Report", 8)
    b.separator()
    b.notice("Jesse Moss", "XXX-XX-1303", "IRS Notice")


@batch("batch_13_intentionally_blank_page", [3, 1])
def _(b):
    b.notice("Cameron Diaz", "XXX-XX-1401", "IRS Notice", 3, left_blank=(2,))
    b.separator()
    b.notice("Logan Pierce", "XXX-XX-1402", "IRS Notice")


@batch(
    "batch_14_scanned_upside_down_separator",
    [1, 1],
    scan={"seed": 14},
    upside_down={1},
)
def _(b):
    b.notice("Marley Quinn", "XXX-XX-1501", "IRS Notice")
    b.separator()
    b.notice("Shawn Ellis", "XXX-XX-1502", "IRS Notice")


@batch("batch_15_scanned_recycled_paper", [1, 2], duplex=True, scan={"seed": 15, "paper": 205})
def _(b):
    b.notice("Jordan Blake", "XXX-XX-1601", "State Notice")
    b.separator()
    b.notice("Alexis Ray", "XXX-XX-1602", "IRS Notice", 2)


@batch("batch_16_damaged_separator", [1, 1], scan={"seed": 16})
def _(b):
    b.notice("Charlie Webb", "XXX-XX-1701", "IRS Notice")
    b.separator(damaged=True)
    b.notice("Skyler Grant", "XXX-XX-1702", "IRS Notice")


IRS_RETURN = ["Department of the Treasury", "Internal Revenue Service", "Ogden, UT 84201-0039"]
IRS_PAYMENT = ["Internal Revenue Service", "P.O. Box 931000", "Louisville, KY 40293-1000"]
CA_FTB = ["STATE OF CALIFORNIA", "FRANCHISE TAX BOARD", "PO BOX 942867", "SACRAMENTO CA 94267-0001"]


@batch("batch_17_notice_variety", [2, 1, 1, 1, 1, 1, 1])
def _(b):
    """
    Realistic layouts for the extractor. Expected values per notice
    live in tests/test_extractor.py (VARIETY_EXPECTED).
    """
    # 1. CP14 balance due, joint filers, masked SSN, payment voucher
    b.formal_notice(
        agency_lines=IRS_RETURN,
        rows=[
            ("Notice", "CP14"),
            ("Tax year", "2023"),
            ("Notice date", "May 20, 2024"),
            ("Social Security number", "XXX-XX-1234"),
            ("To contact us", "800-829-0922"),
            ("Your caller ID", "482913"),
        ],
        name_lines=["JOHN A & JANE B SMITH"],
        title="Amount due: $1,245.80",
        body_lines=["Pay by June 10, 2024 to avoid additional penalties."],
        pages=2,
        footer="Notice CP14 (Rev. 1-2024)",
        payment_address=IRS_PAYMENT,
    )
    b.separator()

    # 2. CP2000 proposed changes, single filer, due date in the body
    b.formal_notice(
        agency_lines=IRS_RETURN,
        rows=[
            ("Notice", "CP2000"),
            ("Tax year", "2022"),
            ("Notice date", "August 5, 2024"),
            ("Social Security number", "XXX-XX-5678"),
            ("AUR control number", "1234567890"),
            ("To contact us", "800-829-8310"),
        ],
        name_lines=["MARIA GARCIA"],
        street="45 OAK AVE APT 2",
        city="SPRINGFIELD, IL 62704",
        title="Proposed changes to your 2022 Form 1040",
        body_lines=["Respond by September 4, 2024."],
        footer="Notice CP2000 (Rev. 3-2023)",
    )
    b.separator()

    # 3. Letter 1058 final notice, names on two lines, tax period date
    b.formal_notice(
        agency_lines=IRS_RETURN,
        rows=[
            ("Letter date", "03/11/2024"),
            ("Taxpayer ID number", "XXX-XX-4321"),
            ("Tax periods", "December 31, 2021"),
            ("Form", "1040"),
        ],
        name_lines=["ROBERT JONES", "LINDA JONES"],
        street="PO BOX 77",
        city="RIVERTON WY 82501",
        title="Final notice of intent to levy",
        footer="Letter 1058 (Rev. 7-2022)",
    )
    b.separator()

    # 4. CP161 business payroll notice, masked EIN, care-of line
    b.formal_notice(
        agency_lines=IRS_RETURN,
        rows=[
            ("Notice", "CP161"),
            ("Tax period", "March 31, 2024"),
            ("Notice date", "June 3, 2024"),
            ("Employer ID number", "XX-XXX6789"),
        ],
        name_lines=["ACME WIDGETS LLC", "% PAT LEE MBR"],
        street="900 INDUSTRIAL PKWY STE 100",
        city="DAYTON, OH 45402-1234",
        title="Request for payment - Form 941",
        footer="Notice CP161 (Rev. 2-2023)",
    )
    b.separator()

    # 5. CP575 EIN assigned: a real notice with no tax year at all
    b.formal_notice(
        agency_lines=IRS_RETURN,
        rows=[
            ("Date of this notice:", "03-15-2024"),
            ("Employer Identification Number:", "12-3456789"),
            ("Form:", "SS-4"),
            ("Number of this notice:", "CP 575 A"),
        ],
        name_lines=["BRIGHT FUTURES FOUNDATION"],
        street="12 ELM ST",
        city="BURLINGTON, VT 05401",
        title="WE ASSIGNED YOU AN EMPLOYER IDENTIFICATION NUMBER",
    )
    b.separator()

    # 6. California FTB notice
    b.formal_notice(
        agency_lines=CA_FTB,
        rows=[
            ("Notice Date:", "04/22/2024"),
            ("Taxable Year:", "2022"),
            ("SSN/ITIN:", "XXX-XX-2468"),
            ("Notice:", "FTB 4502"),
        ],
        name_lines=["DANA WHITE"],
        street="88 PINE RD",
        city="FRESNO CA 93650",
        title="Notice of State Income Tax Due",
    )
    b.separator()

    # 7. Nothing identifiable: must go to the exceptions report
    b.formal_notice(
        agency_lines=[],
        rows=[],
        name_lines=[],
        street="",
        city="",
        title="Thank you for your payment",
        body_lines=["We received your payment and applied it to your account."],
    )


# --- main -----------------------------------------------------------

def main():
    output_dir = config.BASE_DIR / "test-batches" / "qr-separators"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("\nGenerating synthetic test batches...\n")

    for name in BATCHES:
        path = output_dir / f"{name}.pdf"
        with build_batch(name) as doc:
            doc.save(path)
            print(f"Created: {path.name} ({doc.page_count} pages)")

    print(f"\nLocation: {output_dir}")


if __name__ == "__main__":
    main()
