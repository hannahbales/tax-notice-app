
from pathlib import Path
from io import BytesIO

import qrcode
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader, PdfWriter

# save test documents 
OUTPUT = Path(r"C:\TTI-Notice-Dev\intake")
OUTPUT.mkdir(parents=True, exist_ok=True)

PAGE_WIDTH, PAGE_HEIGHT = letter


def create_notice(title, name, tax_id, lines):
    """Generate a fictional tax notice as a PDF page."""
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)

    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(50, 740, title)

    pdf.setFont("Helvetica", 11)
    pdf.drawString(50, 700, f"Taxpayer: {name}")
    pdf.drawString(50, 680, f"Tax ID: {tax_id}")

    y = 640
    for line in lines:
        pdf.drawString(50, y, line)
        y -= 25

    pdf.setFont("Helvetica-Oblique", 9)
    pdf.drawString(50, 50, "FICTIONAL TEST DOCUMENT - NOT AN ACTUAL TAX NOTICE")

    pdf.save()
    buffer.seek(0)
    return buffer


def create_separator():
    """Generate a completely blank white PDF page."""
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)

    pdf.showPage()
    pdf.save()

    buffer.seek(0)
    return buffer


# create individual test pages
pages = [
    create_notice(
        "IRS Notice CP14",
        "John Smith",
        "XXX-XX-1234",
        [
            "Tax Year: 2025",
            "Amount Due: $1,250.00",
            "This is a fictional notice for testing."
        ]
    ),

    create_notice(
        "IRS Notice CP14 - Page 2",
        "John Smith",
        "XXX-XX-1234",
        [
            "Additional information about the notice.",
            "Please retain this page for your records."
        ]
    ),

    create_separator(),

    create_notice(
        "IRS Notice CP2000",
        "Jane Doe",
        "XXX-XX-5678",
        [
            "Tax Year: 2025",
            "Proposed Adjustment: $875.00",
            "This is a fictional notice for testing."
        ]
    ),

    create_separator(),

    create_notice(
        "State Tax Notice",
        "ABC Company",
        "XX-XXX9012",
        [
            "Tax Period: Q2 2025",
            "Amount Due: $3,400.00",
            "This is a fictional business tax notice."
        ]
    ),
]

# combine all pages into one PDF
writer = PdfWriter()

for page_buffer in pages:
    reader = PdfReader(page_buffer)
    for page in reader.pages:
        writer.add_page(page)

output_file = OUTPUT / "test_batch.pdf"

with open(output_file, "wb") as file:
    writer.write(file)

print(f"Created: {output_file}")
print(f"Total pages: {len(writer.pages)}")
