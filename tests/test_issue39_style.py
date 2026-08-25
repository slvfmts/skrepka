"""Public-MVP style_quote/style_range compiler and safety gates (#39)."""

import pytest


def _tab(text="Alpha"):
    return {"body": {"content": [{"startIndex": 1, "endIndex": 1 + len(text),
        "paragraph": {"elements": [{"startIndex": 1, "endIndex": 1 + len(text),
        "textRun": {"content": text}}]}}]}, "namedRanges": {
        "mark": {"namedRanges": [{"namedRangeId": "nr1", "ranges": [
            {"startIndex": 1, "endIndex": 1 + len(text)}]}]}}}


def test_style_quote_compiles_exact_fields_and_false_reset(engine):
    r = engine._resolve_op({"op": "style_quote", "quote": "Alpha", "style": {
        "bold": False, "italic": True, "foreground_color": "#0080FF",
        "font_size_pt": 12, "font_family": "Arial"}}, _tab(), "tab1")
    req = engine._style_request(r)["updateTextStyle"]
    assert req["range"] == {"startIndex": 1, "endIndex": 6, "tabId": "tab1"}
    assert req["textStyle"]["bold"] is False
    assert req["textStyle"]["foregroundColor"]["color"]["rgbColor"]["blue"] == 1
    assert req["fields"] == "bold,italic,foregroundColor,fontSize,weightedFontFamily"


def test_style_range_uses_named_range_identity(engine):
    r = engine._resolve_op({"op": "style_range", "range": "mark", "style": {
        "underline": True}}, _tab(), "tab1")
    assert r["start"] == 1 and r["end"] == 6


@pytest.mark.parametrize("style", [{}, {"bold": "false"}, {"foreground_color": "red"},
                                    {"font_size_pt": 0}, {"font_family": ""},
                                    {"wat": True}])
def test_style_schema_refuses_invalid_before_write(engine, style):
    engine._RAISE_ERRORS = True
    try:
        with pytest.raises(engine.PatchOpError):
            engine._resolve_op({"op": "style_quote", "quote": "Alpha", "style": style},
                               _tab(), "tab1")
    finally:
        engine._RAISE_ERRORS = False


def test_style_warning_ignores_markdown_represented_fields(engine):
    doc = {"body": {"content": [{"paragraph": {"elements": [{"textRun": {
        "content": "x", "textStyle": {"bold": True, "italic": True,
        "link": {"url": "https://example.test"}, "underline": True}}}]}}]}}
    warning = engine._markdown_inline_style_warning(doc)
    assert warning and "underline" in warning and "bold" not in warning


def test_commented_style_rejects_even_explicit_first_occurrence_before_write(engine):
    class Docs:
        batches = []
        def get(self, **_):
            return type("R", (), {"execute": lambda self: {
                "revisionId": "R0", "body": {"content": [{
                    "startIndex": 1, "endIndex": 12,
                    "paragraph": {"elements": [{"startIndex": 1, "endIndex": 6,
                    "textRun": {"content": "Alpha"}}, {"startIndex": 6,
                    "endIndex": 12, "textRun": {"content": " Alpha"}}]}}]}}})()
        def documents(self): return self
        def batchUpdate(self, **_): self.batches.append(True)
    docs = Docs()
    engine._RAISE_ERRORS = True
    try:
        with pytest.raises(engine.PatchOpError):
            engine._apply_op_anchor_safe(docs, None, "doc", {
                "op": "style_quote", "quote": "Alpha", "occurrence": 1,
                "style": {"bold": True}}, None)
    finally:
        engine._RAISE_ERRORS = False
    assert docs.batches == []


def test_style_range_occurrence_is_rejected_before_clean_write(engine, monkeypatch,
                                                               tmp_path, capsys):
    class Docs:
        def __init__(self): self.batches = []
        def documents(self): return self
        def get(self, **_): return type("R", (), {"execute": lambda s: {
            "revisionId": "R0", "body": _tab()["body"],
            "namedRanges": _tab()["namedRanges"]}})()
        def batchUpdate(self, **kw):
            self.batches.append(kw)
            return type("R", (), {"execute": lambda s: {}})()
    docs = Docs()
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: docs)
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments", lambda *_: ([], [], "fp", {}))
    ops = tmp_path / "ops.json"
    ops.write_text('[{"op":"style_range","range":"mark","occurrence":1,"style":{"bold":true}}]', encoding="utf-8")
    with pytest.raises(SystemExit):
        engine.patch_doc("doc", str(ops))
    out = __import__("json").loads(capsys.readouterr().out)
    assert out["ops_applied"] == 0 and docs.batches == []


def test_commented_style_range_occurrence_is_rejected_before_write(engine, monkeypatch,
                                                                   tmp_path):
    class Docs:
        def __init__(self): self.batches = []
        def documents(self): return self
        def get(self, **_): return type("R", (), {"execute": lambda s: {
            "revisionId": "R0", "body": _tab()["body"],
            "namedRanges": _tab()["namedRanges"]}})()
        def batchUpdate(self, **kw): self.batches.append(kw)
    docs = Docs()
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: docs)
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments", lambda *_: (
        [{"id": "c1", "quotedFileContent": {"value": "x"}}],
        [{"id": "c1"}], "fp", {}))
    ops = tmp_path / "ops.json"
    ops.write_text('[{"op":"style_range","range":"mark","occurrence":1,"style":{"bold":true}}]', encoding="utf-8")
    with pytest.raises(SystemExit):
        engine.patch_doc("doc", str(ops))
    assert docs.batches == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_style_size_rejects_nonfinite_direct_values(engine, value):
    engine._RAISE_ERRORS = True
    try:
        with pytest.raises(engine.PatchOpError):
            engine._resolve_op({"op": "style_quote", "quote": "Alpha",
                                "font_size_pt": value}, _tab(), "tab1")
    finally:
        engine._RAISE_ERRORS = False


def test_download_warning_covers_colors_and_font(engine):
    doc = {"body": {"content": [{"paragraph": {"elements": [{"textRun": {
        "content": "x", "textStyle": {"foregroundColor": {"color": {}},
        "backgroundColor": {"color": {}}, "fontSize": {"magnitude": 9}}}}]}}]}}
    warning = engine._markdown_inline_style_warning(doc)
    assert warning and all(x in warning for x in ("text color", "highlight color", "font size"))


def test_patch_doc_commented_style_is_pinned_scoped_and_receipted(
        engine, monkeypatch, tmp_path, capsys):
    class Docs:
        def __init__(self): self.batches = []
        def documents(self): return self
        def get(self, **_): return type("R", (), {"execute": lambda s: {
            "revisionId": "R7", "tabs": [
                {"tabProperties": {"tabId": "t1", "title": "one"},
                 "documentTab": _tab()},
                {"tabProperties": {"tabId": "t2", "title": "two"},
                 "documentTab": _tab()}]}})()
        def batchUpdate(self, **kw):
            self.batches.append(kw["body"])
            return type("R", (), {"execute": lambda s: {}})()
    docs = Docs()
    drive = object()
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: docs)
    monkeypatch.setattr(engine, "get_drive_service", lambda _: drive)
    monkeypatch.setattr(engine, "_census_comments",
                        lambda *_: ([{"id": "c1", "quotedFileContent": {"value": "x"}}],
                                    [{"id": "c1"}], "fp", {}))
    ops = tmp_path / "ops.json"
    ops.write_text('{"bad": 1}', encoding="utf-8")
    ops.write_text('[{"op":"style_quote","quote":"Alpha","style":{"bold":true}}]', encoding="utf-8")
    engine.patch_doc("doc", str(ops), tab_id="t2")
    out = __import__("json").loads(capsys.readouterr().out)
    assert out["strategy"] == "anchor-safe-per-op" and out["ops_applied"] == 1
    req = docs.batches[0]["requests"][0]["updateTextStyle"]
    assert req["range"]["tabId"] == "t2"
    assert docs.batches[0]["writeControl"] == {"requiredRevisionId": "R7"}
    assert "skrepka-canary" not in str(docs.batches)


def test_patch_doc_commented_style_revision_conflict_is_refused(engine, monkeypatch,
                                                                tmp_path, capsys):
    class Docs:
        def documents(self): return self
        def get(self, **_): return type("R", (), {"execute": lambda s: {
            "revisionId": "R7", "body": _tab()["body"]}})()
        def batchUpdate(self, **_):
            import httplib2
            from googleapiclient.errors import HttpError
            return type("R", (), {"execute": lambda s: (_ for _ in ()).throw(
                HttpError(httplib2.Response({"status": "409"}), b"conflict"))})()
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: Docs())
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments",
                        lambda *_: ([{"id": "c1", "quotedFileContent": {"value": "x"}}],
                                    [{"id": "c1"}], "fp", {}))
    ops = tmp_path / "ops.json"
    ops.write_text('[{"op":"style_quote","quote":"Alpha","style":{"bold":true}}]', encoding="utf-8")
    with pytest.raises(SystemExit):
        engine.patch_doc("doc", str(ops), tab_id=None)
    assert '"refused"' in capsys.readouterr().out
