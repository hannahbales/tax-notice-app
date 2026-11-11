"""
Classify each scanned page as a separator, a blank page, or content.

Separator: the page carries a QR code whose payload starts with
SEPARATOR_PREFIX. Only our own payload counts, so QR codes printed
on IRS/state notices never trigger a split.

Blank: tuned for real scans, not clean digital PDFs. It ignores:
  - gray / off-white / recycled paper (measures ink relative to the
    page's own background instead of against pure white)
  - scanner edge shadows, skew wedges, and punch holes (ignores a
    margin band around the page)
  - speckle noise and light bleed-through from the other side (only
    pixels well darker than the background count as ink)

Blank pages never split a notice. They are dropped, which handles the
blank back sides that duplex scanning produces. When unsure, a page is
kept as content: an extra page is harmless, a lost page is not.
"""

from dataclasses import dataclass, field

import cv2
import numpy as np
import pymupdf

from separator_sheet import SEPARATOR_PREFIX

CONTENT = "content"
SEPARATOR = "separator"
BLANK = "blank"

# --- tunables ------------------------------------------------------

# Resolution used to inspect pages. 100 DPI is plenty for a 2.8"
# QR code, and the downsampling averages away 300 DPI scanner speckle.
RENDER_DPI = 100

# Higher resolution second look, used only when a QR-like mark is
# found but cannot be decoded.
RETRY_QR_DPI = 200

# Fraction of width/height ignored on every edge (edge shadows,
# skew wedges, punch holes, staple marks).
MARGIN_RATIO = 0.07

# A pixel is "ink" if it is at least this many gray levels darker
# than the page's background. Light bleed-through stays below this.
INK_DELTA = 80

# Pages with less ink than this fraction are blank. Simulated scans
# of blank sheets (noise, speckle, bleed-through, holes, shadows)
# measure ~0.00%; a single short 10pt line measures ~0.08%. Set low
# because dropping a real page is worse than keeping a blank one.
# Re-check against real scans: see "ink_percent" in manifest.json.
BLANK_INK_RATIO = 0.0002

# Background darker than this means a bad scan (lid open, black
# page, inverted image). It is never treated as blank.
DARK_BACKGROUND_LEVEL = 128


@dataclass
class PageInfo:
    index: int  # 0-based page index in the batch
    kind: str  # CONTENT, SEPARATOR or BLANK
    ink_ratio: float
    notes: list = field(default_factory=list)


def render_gray(page, dpi):
    """Render a page to a 2-D uint8 grayscale numpy array."""
    pix = page.get_pixmap(colorspace=pymupdf.csGRAY, dpi=dpi, alpha=False)
    image = np.frombuffer(pix.samples, dtype=np.uint8)
    return image.reshape(pix.height, pix.stride)[:, : pix.width]


def measure_ink(gray):
    """
    Return (ink_ratio, background_level) for the page's core area.
    """
    height, width = gray.shape
    margin_y = int(height * MARGIN_RATIO)
    margin_x = int(width * MARGIN_RATIO)

    core = gray[margin_y : height - margin_y, margin_x : width - margin_x]
    if core.size == 0:
        core = gray

    background = float(np.median(core))
    ink_pixels = np.count_nonzero(core < background - INK_DELTA)

    return ink_pixels / core.size, background


def _decode_with(detector, gray):
    try:
        found, texts, _points, _ = detector.detectAndDecodeMulti(gray)
    except cv2.error:
        return [], 0

    if not found:
        return [], 0

    texts = [text.strip() for text in texts]
    decoded = [text for text in texts if text]
    return decoded, len(texts) - len(decoded)


def find_qr_codes(gray, thorough=False):
    """
    Return (decoded_payloads, undecoded_count) for QR codes on a page.
    """
    decoded, undecoded = _decode_with(cv2.QRCodeDetector(), gray)

    if thorough and undecoded:
        # the ArUco-based detector copes better with damaged codes.
        more, still_undecoded = _decode_with(cv2.QRCodeDetectorAruco(), gray)
        decoded += more
        undecoded = still_undecoded if more else undecoded

    return decoded, undecoded


def is_separator_payload(text):
    return text.startswith(SEPARATOR_PREFIX)


def classify_page(page, index):
    """Classify one PyMuPDF page."""
    gray = render_gray(page, RENDER_DPI)
    ink_ratio, background = measure_ink(gray)
    notes = []

    payloads, undecoded = find_qr_codes(gray)

    if undecoded and not any(map(is_separator_payload, payloads)):
        # something QR-like was found but not read. Look closer
        # before deciding; a damaged separator must not be missed.
        payloads, undecoded = find_qr_codes(
            render_gray(page, RETRY_QR_DPI), thorough=True
        )

    if any(map(is_separator_payload, payloads)):
        return PageInfo(index, SEPARATOR, ink_ratio)

    if undecoded:
        notes.append(
            "has an unreadable QR code (possibly a damaged separator sheet)"
        )

    if background < DARK_BACKGROUND_LEVEL:
        notes.append("is mostly dark (possible scan problem)")
        return PageInfo(index, CONTENT, ink_ratio, notes)

    if ink_ratio < BLANK_INK_RATIO:
        return PageInfo(index, BLANK, ink_ratio, notes)

    return PageInfo(index, CONTENT, ink_ratio, notes)
