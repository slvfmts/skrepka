import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

WORDML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@pytest.fixture
def engine():
    import skrepka._engine as eng
    return eng


@pytest.fixture
def make_docx():
    """Build a minimal docx (word/document.xml only) from a body-XML string."""
    def _make(body_xml: str) -> bytes:
        doc = (f'<?xml version="1.0"?>'
               f'<w:document xmlns:w="{WORDML}"><w:body>{body_xml}</w:body>'
               f'</w:document>')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", doc)
        return buf.getvalue()
    return _make


@pytest.fixture
def doc_tab():
    """Synthetic documentTab factory: list of (start, end, text[, textRun-extras])."""
    def _make(runs):
        elements = []
        for r in runs:
            start, end, text = r[0], r[1], r[2]
            tr = {"content": text}
            if len(r) > 3:
                tr.update(r[3])
            elements.append({"startIndex": start, "endIndex": end,
                             "textRun": tr})
        return {"body": {"content": [
            {"startIndex": runs[0][0], "endIndex": runs[-1][1],
             "paragraph": {"elements": elements}}]}}
    return _make
