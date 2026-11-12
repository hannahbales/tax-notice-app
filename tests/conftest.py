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

        with pymupdf.open(stream=body.read(), filetype="pdf") as doc:
            texts = [page.get_text() for page in doc]

        texts = texts[: self.max_pages]

        return FakePoller(
            FakeResult(
                apiVersion="fake",
                modelId=model_id,
                content="\n".join(texts),
                pages=[
                    {
                        "pageNumber": number,
                        "words": [
                            {"content": w, "confidence": self.confidence}
                            for w in text.split()
                        ],
                    }
                    for number, text in enumerate(texts, start=1)
                ],
            )
        )


@pytest.fixture(autouse=True)
def fake_azure(monkeypatch):
    """No test ever talks to the real Azure service."""
    fake = FakeAzure()
    monkeypatch.setattr(ocr, "get_client", lambda: fake)
    yield fake
