import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402


@pytest.fixture(autouse=True)
def tti_dirs(tmp_path):
    """Run every test against a throwaway folder tree, never C:\\TTI-Notice-Dev."""
    original = config.BASE_DIR
    config.set_base_dir(tmp_path / "tti")
    config.INTAKE_DIR.mkdir(parents=True)
    yield config
    config.set_base_dir(original)
