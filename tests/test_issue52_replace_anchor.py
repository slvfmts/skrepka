"""Focused adversarial coverage for issue #52's thread-addressed rewrite."""

import pytest

from test_sync_anchors import (  # noqa: E402
    CREATED,
    CREATED_SEC,
    DocsStub,
    DriveStub,
    _docx_builder,
    api_comment,
    make_doc,
    wire,
)


def _duplicate_anchor(monkeypatch, engine, comment=None):
    doc = make_doc(["Alpha", "Alpha", "Charlie"])
    docs = DocsStub(doc)
    drive = DriveStub(
        [comment or api_comment("c1", "A", CREATED)],
        _docx_builder(
            docs,
            [("Alpha", []), ("Alpha", [("0", 0, 5)]),
             ("Charlie", [])],
            [("0", "A", CREATED_SEC)],
        ),
    )
    wire(engine, monkeypatch, docs, drive)
    return docs, drive


def test_replace_anchor_rewrites_only_the_docx_mapped_duplicate(
        engine, monkeypatch):
    docs, drive = _duplicate_anchor(monkeypatch, engine)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    out = engine._apply_op_anchor_safe(
        docs, drive, "doc1",
        {"op": "replace_anchor", "comment_id": "c1", "with": "Zulu"},
        None)
    assert out["applied_as"] == "exact-anchor"
    semantic = docs.batches[-1]
    assert any("replaceAllText" in r for r in semantic)
    assert semantic[0].get("deleteContentRange")  # canary first
    match = next(r["replaceAllText"] for r in semantic
                 if "replaceAllText" in r)
    needle = match["containsText"]["text"]
    assert needle.startswith("Alphskrepka-anchor-sentinel-")
    assert match["replaceText"] == "Zulu"
    assert docs.canary_text is None


@pytest.mark.parametrize("op", [
    {"op": "replace_anchor", "comment_id": "missing", "with": "Zulu"},
    {"op": "replace_anchor", "comment_id": "c1", "with": ""},
])
def test_replace_anchor_refuses_unknown_or_empty_without_semantic_write(
        engine, monkeypatch, op):
    docs, drive = _duplicate_anchor(monkeypatch, engine)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    with pytest.raises(engine.PatchOpError):
        engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    # Unknown id is rejected before canary; empty replacement has only the
    # canary insert/cleanup pair and never a semantic batch.
    assert not any(any("replaceAllText" in request for request in batch)
                   for batch in docs.batches)
    assert docs.canary_text is None


def test_replace_anchor_rejects_resolved_thread_before_canary(
        engine, monkeypatch):
    docs, drive = _duplicate_anchor(
        monkeypatch, engine,
        comment=api_comment("c1", "A", CREATED, resolved=True),
    )
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    with pytest.raises(engine.PatchOpError, match="resolved"):
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_anchor", "comment_id": "c1", "with": "Zulu"},
            None)
    assert docs.batches == []


def test_replace_anchor_comment_race_cleans_canary_before_refusing(
        engine, monkeypatch):
    docs, drive = _duplicate_anchor(monkeypatch, engine)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    monkeypatch.setattr(engine, "_comments_fingerprint",
                        lambda *_args: {("race",)})
    with pytest.raises(engine.PatchOpError, match="comments changed"):
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_anchor", "comment_id": "c1", "with": "Zulu"},
            None)
    assert docs.canary_text is None


def test_replace_anchor_shape_is_strict(engine, doc_tab, monkeypatch):
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    with pytest.raises(engine.PatchOpError, match="unknown replace_anchor"):
        engine._resolve_op(
            {"op": "replace_anchor", "comment_id": "c1",
             "with": "x", "quote": "stale"},
            doc_tab([(1, 3, "x\n")]), None)
    with pytest.raises(engine.PatchOpError, match="requires string"):
        engine._resolve_op(
            {"op": "replace_anchor", "comment_id": "c1", "with": None},
            doc_tab([(1, 3, "x\n")]), None)


def test_exact_anchor_semantic_batch_scopes_all_requests(engine):
    class Recorder:
        def __init__(self):
            self.body = None

        def documents(self):
            return self

        def batchUpdate(self, documentId=None, body=None):
            del documentId
            self.body = body

            class Result:
                def execute(inner):
                    del inner
                    return {"replies": [{}, {},
                                        {"replaceAllText": {
                                            "occurrencesChanged": 1}}]}

            return Result()

    docs = Recorder()
    engine._execute_exact_anchor_rewrite(
        docs, "doc1", "child", [
            {"insertText": {"location": {"index": 4}, "text": "x"}},
            {"replaceAllText": {"containsText": {"text": "x"},
                                 "replaceText": "y"}},
            {"deleteContentRange": {"range": {"startIndex": 4,
                                                "endIndex": 5}}},
        ], "R1", "test")
    for request in docs.body["requests"]:
        payload = next(iter(request.values()))
        holder = (payload.get("location") or payload.get("range")
                  or payload.get("tabsCriteria"))
        if "tabsCriteria" in payload:
            assert payload["tabsCriteria"] == {"tabIds": ["child"]}
        else:
            assert holder["tabId"] == "child"


def test_exact_anchor_request_builder_refuses_empty_replacement(engine):
    assert engine._exact_anchor_rewrite_requests("Alpha", "", 1, 6) is None


def test_duplicate_ordinal_ignores_nested_table_paragraphs(engine):
    # The flattened DOCX paragraph index is deliberately absurd (the table
    # has six cell paragraphs before the marker). The top-level outline still
    # identifies the second of four identical body paragraphs exactly.
    snapshot = {
        "docx_outline": [
            {"kind": "tbl"}, {"kind": "p", "text": "Alpha"},
            {"kind": "p", "text": "Alpha"},
            {"kind": "p", "text": "Alpha"},
            {"kind": "p", "text": "Alpha"},
        ],
        "segment_first": 0,
        "segment_last": None,
    }
    marker = {"para_index": 8, "top": 2, "para_text": "Alpha",
              "candidates": [(1, 7), (7, 13), (13, 19), (19, 25)]}
    assert engine._top_level_marker_ordinal(snapshot, marker) == 1


@pytest.mark.parametrize("operation, marker_top, expected", [
    ("replace_anchor", 3, {"startIndex": 8, "endIndex": 9}),
    ("replace_anchor", 5, {"startIndex": 19, "endIndex": 20}),
    ("replace_around_anchor", 3, {"startIndex": 8, "endIndex": 9}),
    ("replace_around_anchor", 5, {"startIndex": 19, "endIndex": 20}),
])
def test_exact_text_ordinal_excludes_title_table_and_non_twins(
        engine, monkeypatch, operation, marker_top, expected):
    """Only exact twin paragraph text may determine the API candidate ordinal."""
    doc = make_doc(["Intro", "Alpha", "Note", "Alpha", "Tail"])
    docs = DocsStub(doc)
    drive = DriveStub([api_comment("c1", "A", CREATED)], lambda: b"unused")
    wire(engine, monkeypatch, docs, drive)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    snapshot = {
        "anchors": [],
        "ambiguous": [{
            "docx_id": "0", "para_index": 99, "top": marker_top,
            "end_top": marker_top, "para_text": "Alpha", "start_off": 0,
            "end_off": 5, "candidates": [(7, 13), (18, 24)],
        }],
        "docx_outline": [
            {"kind": "title", "text": "Synthetic title"},
            {"kind": "tbl", "text": "Alpha"},
            {"kind": "p", "text": "Intro"},
            {"kind": "p", "text": "Alpha"},
            {"kind": "p", "text": "Note"},
            {"kind": "p", "text": "Alpha"},
            {"kind": "p", "text": "Tail"},
        ],
        "segment_first": 0, "segment_last": None,
        "blocked": [], "attribution": {"0": "c1"}, "fp1": set(),
        "canary": {"text": "canary", "start": 40, "end": 46,
                   "tab_id": None}, "r1": "R1",
    }
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot",
                        lambda *args, **kwargs: (snapshot, None))
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *args: True)
    monkeypatch.setattr(engine, "_comments_fingerprint", lambda *args: set())
    if operation == "replace_around_anchor":
        monkeypatch.setattr(
            engine, "_replace_around_anchor_plan",
            lambda _doc, _op, start, end, *_rest: ((
                engine._exact_anchor_rewrite_requests(
                    "Alpha", "Z", start, end), {"old_anchor_length_utf16": 5,
                                                   "new_anchor_length_utf16": 1}),
                None))
        op = {"op": operation, "comment_id": "c1", "with": "Z",
              "before_utf16": 1, "after_utf16": 0,
              "quote": "Alpha", "anchor": {"text": "Z",
                                               "start_utf16": 0}}
    else:
        op = {"op": operation, "comment_id": "c1", "with": "Z"}
    out = engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    assert out["applied_as"] in {"exact-anchor", "around-anchor"}
    deletes = [r["deleteContentRange"]["range"] for r in docs.batches[-1]
               if "deleteContentRange" in r]
    assert expected in deletes
    wrong = {"startIndex": 8, "endIndex": 9} if marker_top == 5 else {
        "startIndex": 19, "endIndex": 20}
    assert wrong not in deletes


def test_exact_text_ordinal_refuses_unenumerated_non_twin_mapping(engine):
    snapshot = {
        "docx_outline": [{"kind": "p", "text": "Alpha"},
                         {"kind": "p", "text": "Note"}],
        "segment_first": 0, "segment_last": None,
    }
    marker = {"top": 0, "para_text": "Alpha",
              "candidates": [(1, 7), (7, 13)]}
    assert engine._top_level_marker_ordinal(snapshot, marker) == -1


def test_duplicate_reply_marker_descriptors_are_deduped_but_ranges_not(
        engine):
    attribution = {"root": "c1", "reply": "c1", "other": "c1"}
    same = {"para_text": "Alpha", "top": 2, "end_top": 2,
            "start_off": 0, "end_off": 5,
            "candidates": [(1, 7)], "docx_id": "root"}
    duplicate = {**same, "docx_id": "reply"}
    different = {**same, "docx_id": "other", "top": 3,
                 "candidates": [(7, 13)]}
    out = engine._dedupe_anchor_descriptors(
        [same, duplicate, different], attribution, "c1")
    assert len(out) == 2
    assert {item["top"] for item in out} == {2, 3}


def test_table_before_duplicates_rewrites_the_second_top_level_copy(
        engine, monkeypatch):
    # Exercise the complete target selection/write path with the same
    # evidence shape produced by the DOCX parser: a table is one outline
    # element but contributes many flattened cell paragraphs.
    doc = make_doc(["Alpha", "Alpha", "Alpha", "Alpha"])
    docs = DocsStub(doc)
    drive = DriveStub([api_comment("c1", "A", CREATED)],
                      lambda: b"unused")
    wire(engine, monkeypatch, docs, drive)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    snapshot = {
        "anchors": [],
        "ambiguous": [{
            "docx_id": "0", "para_index": 8, "top": 2,
            "end_top": 2, "para_text": "Alpha", "start_off": 0,
            "end_off": 5, "candidates": [(1, 7), (7, 13),
                                           (13, 19), (19, 25)],
        }, {
            "docx_id": "reply", "para_index": 8, "top": 2,
            "end_top": 2, "para_text": "Alpha", "start_off": 0,
            "end_off": 5, "candidates": [(1, 7), (7, 13),
                                           (13, 19), (19, 25)],
        }],
        "docx_outline": [{"kind": "tbl"},
                         {"kind": "p", "text": "Alpha"},
                         {"kind": "p", "text": "Alpha"},
                         {"kind": "p", "text": "Alpha"},
                         {"kind": "p", "text": "Alpha"}],
        "segment_first": 0, "segment_last": None,
        "blocked": [(7, 12, "combined ambiguous fence (и ещё...)")],
        "blocked_sources": [
            {"start": 7, "end": 12, "source": "ambiguous",
             "owners": {"c1"}, "docx_id": "0"},
            {"start": 7, "end": 12, "source": "ambiguous",
             "owners": {"c1"}, "docx_id": "reply"},
        ],
        "attribution": {"0": "c1", "reply": "c1"}, "fp1": set(),
        "canary": {"text": "canary", "start": 30, "end": 36,
                   "tab_id": None}, "r1": "R1",
    }
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot",
                        lambda *args, **kwargs: (snapshot, None))
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *args: True)
    monkeypatch.setattr(engine, "_comments_fingerprint", lambda *args: set())
    out = engine._apply_op_anchor_safe(
        docs, drive, "doc1",
        {"op": "replace_anchor", "comment_id": "c1", "with": "Z"}, None)
    assert out["applied_as"] == "exact-anchor"
    semantic = docs.batches[-1]
    deletes = [r["deleteContentRange"]["range"] for r in semantic
               if "deleteContentRange" in r]
    # Target is [7, 13), not first [1, 7) or third [13, 19).
    assert {"startIndex": 8, "endIndex": 9} in deletes
    assert {"startIndex": 2, "endIndex": 3} not in deletes


def test_foreign_marker_same_range_refuses_before_semantic_write(
        engine, monkeypatch):
    doc = make_doc(["Alpha", "Alpha", "Alpha"])
    docs = DocsStub(doc)
    drive = DriveStub([api_comment("c1", "A", CREATED)], lambda: b"unused")
    wire(engine, monkeypatch, docs, drive)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    snapshot = {
        "anchors": [],
        "ambiguous": [
            {"docx_id": "0", "para_index": 1, "top": 0,
             "end_top": 0, "para_text": "Alpha", "start_off": 0,
             "end_off": 5, "candidates": [(1, 7), (7, 13)]},
            {"docx_id": "other", "para_index": 1, "top": 0,
             "end_top": 0, "para_text": "Alpha", "start_off": 0,
             "end_off": 5, "candidates": [(1, 7), (7, 13)]},
        ],
        "docx_outline": [{"kind": "p", "text": "Alpha"},
                         {"kind": "p", "text": "Alpha"}],
        "segment_first": 0, "segment_last": None,
        "blocked": [(1, 6, "combined ambiguous fence (и ещё...)")],
        "attribution": {"0": "c1", "other": "c2"},
        "fp1": set(),
        "canary": {"text": "canary", "start": 20, "end": 26,
                   "tab_id": None}, "r1": "R1",
    }
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot",
                        lambda *args, **kwargs: (snapshot, None))
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *args: True)
    with pytest.raises(engine.PatchOpError, match="protected"):
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_anchor", "comment_id": "c1", "with": "Z"},
            None)
    assert not any("replaceAllText" in request for batch in docs.batches
                   for request in batch)


@pytest.mark.parametrize("operation, source", [
    ("replace_anchor", "ghost"),
    ("replace_around_anchor", "ghost"),
    ("replace_anchor", "table"),
    ("replace_around_anchor", "table"),
])
def test_merged_non_ambiguous_fence_source_is_never_lifted(
        engine, monkeypatch, operation, source):
    doc = make_doc(["Alpha", "Alpha"])
    docs = DocsStub(doc)
    drive = DriveStub([api_comment("c1", "A", CREATED)], lambda: b"unused")
    wire(engine, monkeypatch, docs, drive)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    descriptor = {"docx_id": "target", "para_index": 1, "top": 0,
                  "end_top": 0, "para_text": "Alpha", "start_off": 0,
                  "end_off": 5, "candidates": [(1, 7), (7, 13)]}
    snapshot = {
        "anchors": [], "ambiguous": [descriptor],
        "docx_outline": [{"kind": "p", "text": "Alpha"},
                         {"kind": "p", "text": "Alpha"}],
        "segment_first": 0, "segment_last": None,
        "blocked": [(1, 6, "combined ambiguous fence (и ещё...)")],
        "blocked_sources": [
            {"start": 1, "end": 6, "source": "ambiguous",
             "owners": {"c1"}, "docx_id": "target"},
            {"start": 1, "end": 6, "source": source,
             "owners": {"c2"} if source == "ghost" else set()},
        ],
        "attribution": {"target": "c1"}, "fp1": set(),
        "canary": {"text": "canary", "start": 20, "end": 26,
                   "tab_id": None}, "r1": "R1",
    }
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot",
                        lambda *args, **kwargs: (snapshot, None))
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *args: True)
    monkeypatch.setattr(engine, "_comments_fingerprint", lambda *args: set())
    if operation == "replace_around_anchor":
        monkeypatch.setattr(
            engine, "_replace_around_anchor_plan",
            lambda *_args: (None, "protected fence"))
        op = {"op": operation, "comment_id": "c1", "with": "Z",
              "before_utf16": 1, "after_utf16": 0, "quote": "Alpha",
              "anchor": {"text": "Z", "start_utf16": 0}}
    else:
        op = {"op": operation, "comment_id": "c1", "with": "Z"}
    with pytest.raises(engine.PatchOpError, match="protected"):
        engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    assert not any("replaceAllText" in request for batch in docs.batches
                   for request in batch)
