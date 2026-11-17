import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
import ocr  # noqa: E402


@pytest.fixture(autouse=True)
def tti_dirs(tmp_path):
    """Run every test against a throwaway folder tree, never C:\\TTI-Notice-Dev."""
    original = config.BASE_DIR
    config.set_base_dir(tmp_path / "tti")
    config.INTAKE_DIR.mkdir(parents=True)
    yield config
    config.set_base_dir(original)


# --- fake Azure -------------------------------------------------------

class FakePoller:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class FakeResult(dict):
    def as_dict(self):
        return dict(self)


class FakeAzure:
    """
    Stands in for DocumentIntelligenceClient. "Reads" the PDF's text
    layer with PyMuPDF, so results look like real OCR output.
    """

    def __init__(self):
        self.calls = 0
        self.max_pages = None  # set to 2 to mimic the free tier
        self.error = None  # set to an exception to simulate failure
        self.confidence = 0.99

    def begin_analyze_document(self, model_id, body):
        self.calls += 1

        if self.error is not None:
            raise self.error

        pages = []
        with pymupdf.open(stream=body.read(), filetype="pdf") as doc:
            for number, page in enumerate(doc, start=1):
                if self.max_pages and number > self.max_pages:
                    break
                pages.append(_fake_page(page, number, self.confidence))

        return FakePoller(
            FakeResult(
                apiVersion="fake",
                modelId=model_id,
                content="\n".join(line["content"] for p in pages for line in p["lines"]),
                pages=pages,
            )
        )


def _fake_page(page, number, confidence):
    """Azure-shaped page: lines with polygons in inches, plus words."""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if not text:
                continue
            x0, y0, x1, y1 = (v / 72 for v in line["bbox"])
            lines.append(
                {"content": text, "polygon": [x0, y0, x1, y0, x1, y1, x0, y1]}
            )

    return {
        "pageNumber": number,
        "width": page.rect.width / 72,
        "height": page.rect.height / 72,
        "unit": "inch",
        "lines": lines,
        "words": [
            {"content": word, "confidence": confidence}
            for line in lines
            for word in line["content"].split()
        ],
    }


@pytest.fixture(autouse=True)
def fake_azure(monkeypatch):
    """No test ever talks to the real Azure service."""
    fake = FakeAzure()
    monkeypatch.setattr(ocr, "get_client", lambda: fake)
    yield fake
