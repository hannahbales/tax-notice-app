
from pathlib import Path
from io import BytesIO
from datetime import datetime

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from pypdf import PdfReader, PdfWriter


# configuration

BASE_DIR = Path(r"C:\TTI-Notice-Dev")
OUTPUT_DIR = BASE_DIR / "test-batches"

PAGE_WIDTH, PAGE_HEIGHT = letter


# fictional notice page

def create_notice_page(
    client_name,
    tax_id,
    notice_type,
    page_number,
    total_pages,
    batch_id,
    sparse=False,
):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)

    if sparse:
        # intentionally minimal text for blank-page testing
        pdf.setFont("Helvetica", 10)
        pdf.drawString(
            72,
            PAGE_HEIGHT - 72,
            f"Test notice for {client_name}",
        )

    else:
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawString(
            72,
            PAGE_HEIGHT - 72,
            "SYNTHETIC TAX NOTICE - TEST ONLY",
        )

        pdf.setFont("Helvetica", 11)

        lines = [
            f"Batch: {batch_id}",
            f"Client: {client_name}",
            f"Tax ID: {tax_id}",
            f"Notice Type: {notice_type}",
            f"Page: {page_number} of {total_pages}",
            f"Generated: {datetime.now().isoformat()}",
            "",
            "This is fictional test data.",
            "No real taxpayer information is included.",
            "",
            "Please retain this notice for your records.",
        ]

        y = PAGE_HEIGHT - 110

        for line in lines:
            pdf.drawString(72, y, line)
            y -= 22

    pdf.showPage()
    pdf.save()

    buffer.seek(0)
    return buffer


# blank separator page

def create_blank_page():
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)

    # do not draw anything on this page
    pdf.showPage()
    pdf.save()

    buffer.seek(0)
    return buffer


# add pgs to batch

def add_pdf_page(writer, pdf_buffer):
    reader = PdfReader(pdf_buffer)
    writer.add_page(reader.pages[0])


def add_notice(
    writer,
    client_name,
    tax_id,
    notice_type,
    page_count,
    batch_id,
    sparse=False,
):
    for page_number in range(1, page_count + 1):
        page = create_notice_page(
            client_name=client_name,
            tax_id=tax_id,
            notice_type=notice_type,
            page_number=page_number,
            total_pages=page_count,
            batch_id=batch_id,
            sparse=sparse,
        )

        add_pdf_page(writer, page)


def add_separator(writer, count=1):
    for _ in range(count):
        add_pdf_page(writer, create_blank_page())


# save batch without overwriting existing files

def save_batch(filename, writer):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    output_path = OUTPUT_DIR / filename

    with output_path.open("xb") as file:
        writer.write(file)

    print(
        f"Created: {output_path.name} "
        f"({len(writer.pages)} pages)"
    )


# --------------------------------------------------
# Batch 1: Standard batch
# --------------------------------------------------

def batch_01():
    writer = PdfWriter()

    add_notice(
        writer, "John Smith", "XXX-XX-1001",
        "IRS Balance Due", 2, "BATCH-01",
    )

    add_separator(writer)

    add_notice(
        writer, "Jane Doe", "XXX-XX-1002",
        "IRS Penalty Notice", 1, "BATCH-01",
    )

    add_separator(writer)

    add_notice(
        writer, "ABC Company", "XX-XXX1003",
        "Payroll Tax Notice", 1, "BATCH-01",
    )

    save_batch("batch_01_standard.pdf", writer)


# --------------------------------------------------
# Batch 2: Single notice
# --------------------------------------------------

def batch_02():
    writer = PdfWriter()

    add_notice(
        writer, "Taylor Johnson", "XXX-XX-2001",
        "IRS Payment Reminder", 2, "BATCH-02",
    )

    save_batch("batch_02_single.pdf", writer)


# --------------------------------------------------
# Batch 3: Multi-page notices
# --------------------------------------------------

def batch_03():
    writer = PdfWriter()

    add_notice(
        writer, "Morgan Lee", "XXX-XX-3001",
        "IRS Examination Notice", 3, "BATCH-03",
    )

    add_separator(writer)

    add_notice(
        writer, "Jordan Davis", "XXX-XX-3002",
        "IRS Collection Notice", 3, "BATCH-03",
    )

    save_batch("batch_03_multipage.pdf", writer)


# --------------------------------------------------
# Batch 4: Larger batch
# --------------------------------------------------

def batch_04():
    writer = PdfWriter()

    clients = [
        ("Alex Rivera", "XXX-XX-4001", 1),
        ("Sam Parker", "XXX-XX-4002", 2),
        ("Chris Taylor", "XXX-XX-4003", 3),
        ("Jamie Wilson", "XXX-XX-4004", 1),
        ("Casey Brown", "XXX-XX-4005", 2),
    ]

    for index, (name, tax_id, pages) in enumerate(clients):
        if index > 0:
            add_separator(writer)

        add_notice(
            writer,
            name,
            tax_id,
            "IRS Tax Notice",
            pages,
            "BATCH-04",
        )

    save_batch("batch_04_large.pdf", writer)


# --------------------------------------------------
# Batch 5: Consecutive blank pages
# --------------------------------------------------

def batch_05():
    writer = PdfWriter()

    add_notice(
        writer, "Robin Miller", "XXX-XX-5001",
        "IRS Notice", 2, "BATCH-05",
    )

    add_separator(writer, count=2)

    add_notice(
        writer, "Drew Anderson", "XXX-XX-5002",
        "IRS Notice", 1, "BATCH-05",
    )

    save_batch(
        "batch_05_consecutive_blanks.pdf",
        writer,
    )


# --------------------------------------------------
# Batch 6: Sparse text
# --------------------------------------------------

def batch_06():
    writer = PdfWriter()

    add_notice(
        writer, "Avery Clark", "XXX-XX-6001",
        "Sparse Notice", 1, "BATCH-06",
        sparse=True,
    )

    add_separator(writer)

    add_notice(
        writer, "Riley Brooks", "XXX-XX-6002",
        "Sparse Notice", 1, "BATCH-06",
        sparse=True,
    )

    save_batch("batch_06_sparse.pdf", writer)


# --------------------------------------------------
# Batch 7: Blank-only PDF
# --------------------------------------------------

def batch_07():
    writer = PdfWriter()

    add_separator(writer, count=3)

    save_batch("batch_07_blank_only.pdf", writer)


# generate all batches

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("\nGenerating synthetic test batches...\n")

    batch_01()
    batch_02()
    batch_03()
    batch_04()
    batch_05()
    batch_06()
    batch_07()

    print("\nAll test batches generated!")
    print(f"Location: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
