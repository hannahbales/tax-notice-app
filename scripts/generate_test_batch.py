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
