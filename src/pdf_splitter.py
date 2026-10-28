from pathlib import Path
from datetime import datetime
import shutil
from io import BytesIO
import pymupdf
from PIL import Image
from pypdf import PdfReader, PdfWriter


BASE_DIR = Path(r"C:\TTI-Notice-Dev")
INPUT_FILE = BASE_DIR / "intake" / "test_batch.pdf"
OUTPUT_DIR = BASE_DIR / "working" / "split_notices"
ARCHIVE_DIR = BASE_DIR / "original-batches"

# adjustable blank-page detection settings
WHITE_THRESHOLD = 245
BLANK_PAGE_RATIO = 0.995


def is_blank_page(page):
    """
    Determine whether a page is visually blank.

    A page is considered blank if at least 99.5%
    of its grayscale pixels are nearly white.
    """

    # render the PDF page as a grayscale image
    pix = page.get_pixmap(
        colorspace=pymupdf.csGRAY,
        dpi=100,
        alpha=False
    )

    image = Image.frombytes(
        "L",
        (pix.width, pix.height),
        pix.samples
    )

    # count pixels that are nearly white
    histogram = image.histogram()

    white_pixels = sum(
        histogram[WHITE_THRESHOLD:]
    )

    total_pixels = image.width * image.height

    white_ratio = white_pixels / total_pixels

    print(f"  White ratio: {white_ratio:.4%}")

    return white_ratio >= BLANK_PAGE_RATIO


def save_notice(pages, notice_number):
    """Save a group of pages as a separate PDF."""

    writer = PdfWriter()

    for page in pages:
        writer.add_page(page)

    output_path = (
        OUTPUT_DIR / f"notice_{notice_number:03d}.pdf"
    )

    if output_path.exists():
        raise FileExistsError(
            f"File already exists: {output_path}"
        )

    with open(output_path, "wb") as file:
        writer.write(file)

    print(
        f"Saved {output_path.name} "
        f"({len(pages)} pages)"
    )


def archive_original(input_file):
    """
    Save an untouched copy of the original batch PDF.

    Uses a unique filename to prevent overwriting.
    """

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    archive_path = (
        ARCHIVE_DIR / f"{timestamp}_{input_file.name}"
    )

    # exclusive creation, no overwrites
    with input_file.open("rb") as source:
        with archive_path.open("xb") as destination:
            shutil.copyfileobj(source, destination)

    print(f"Original batch archived: {archive_path.name}")

    return archive_path

def split_pdf():
    """Archive original batch, then split at blank pages."""

    if not INPUT_FILE.is_file():
        print(f"Input PDF not found: {INPUT_FILE}")
        return

    # save og before processing or splitting
    archive_path = archive_original(INPUT_FILE)

    print(f"Archive saved to: {archive_path}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # PyMuPDF: visually inspect pages
    visual_doc = pymupdf.open(INPUT_FILE)

    # pypdf: preserve original pages when splitting
    reader = PdfReader(INPUT_FILE)

    current_notice = []
    notice_number = 1

    print(f"Processing {len(reader.pages)} pages...\n")

    try:
        for index, page in enumerate(reader.pages):

            print(f"Checking page {index + 1}")

            if is_blank_page(visual_doc[index]):

                print("  Blank separator detected!")

                if current_notice:
                    save_notice(
                        current_notice,
                        notice_number
                    )

                    notice_number += 1
                    current_notice = []

            else:
                current_notice.append(page)

        # save final notice
        if current_notice:
            save_notice(current_notice, notice_number)

        print("\nPDF splitting complete!")

    finally:
        visual_doc.close()


if __name__ == "__main__":
    split_pdf()