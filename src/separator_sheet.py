"""
The printed QR separator sheet that staff insert between notices.

The QR payload is what the splitter looks for. Any QR code with a
different payload (e.g. an IRS payment QR code printed on a notice)
is ignored.
"""

from io import BytesIO

import pymupdf
import qrcode

# Bump the version suffix only if the sheet design changes in a way
# old sheets should stop being recognised. Every payload that starts
# with SEPARATOR_PREFIX counts as a separator.
SEPARATOR_PREFIX = "TTI-NOTICE-SEPARATOR"
SEPARATOR_PAYLOAD = f"{SEPARATOR_PREFIX}-V1"

LETTER = pymupdf.paper_rect("letter")


def separator_qr_png():
    """Return PNG bytes of the separator QR code."""
    qr = qrcode.QRCode(
        # H = survives ~30% damage (staples, folds, pen marks).
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(SEPARATOR_PAYLOAD)
    qr.make(fit=True)

    buffer = BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


def add_separator_page(doc):
    """
    Append one separator page to a PyMuPDF document.

    Two QR codes per page so one can be damaged, stapled through,
    or covered without the sheet being missed.
    """
    page = doc.new_page(width=LETTER.width, height=LETTER.height)
    png = separator_qr_png()

    centre = LETTER.width / 2
    size = 200  # points (~2.8 inches)

    page.insert_text(
        (centre - 175, 80),
        "NOTICE SEPARATOR",
        fontsize=36,
        fontname="helv",
    )

    top = pymupdf.Rect(centre - size / 2, 110, centre + size / 2, 110 + size)
    page.insert_image(top, stream=png)

    page.insert_text(
        (centre - 200, 370),
        "Place one sheet between each notice.",
        fontsize=16,
    )
    page.insert_text(
        (centre - 200, 395),
        "Reusable - do not write on or staple the codes.",
        fontsize=16,
    )

    bottom = pymupdf.Rect(centre - size / 2, 430, centre + size / 2, 430 + size)
    page.insert_image(bottom, stream=png)

    return page
