"""Issue #35b: duplicate text is addressable by a named range.

These tests intentionally exercise the commented-document path, where a
``replaceAllText`` fallback would either touch both copies or refuse the
operation as non-unique.
"""

import pytest

from test_sync_anchors import (  # noqa: E402
    CREATED,
    CREATED_SEC,
    DocsStub,
    DriveStub,
    _docx_builder,
    api_comment,
    http_error,
    make_doc,
    wire,
)


def _duplicate_doc(named=None, rev="R0"):
    # 1..7 and 7..13 are two copies of ``Alpha``; mark the second one.
    return make_doc(["Alpha", "Alpha", "Charlie"],
                    named_ranges=named or {
                        "target": {"namedRanges": [{"namedRangeId": "nr-target", "ranges": [{
                            "startIndex": 7, "endIndex": 13,
                        }]}]},
                    }, rev=rev)


def _commented_duplicate(monkeypatch, engine, named=None, comments=None,
                         rev="R0"):
    doc = _duplicate_doc(named, rev=rev)
    docs = DocsStub(doc)
    drive = DriveStub(
        comments or [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []), ("Alpha", []),
                             ("Charlie", [("0", 0, 7)])],
                      [("0", "A", CREATED_SEC)]),
    )
    wire(engine, monkeypatch, docs, drive)
    return docs, drive


def test_duplicate_text_changes_only_named_copy_and_uses_one_index_batch(
        engine, monkeypatch):
    docs, _drive = _commented_duplicate(monkeypatch, engine)
    out = engine._apply_op_anchor_safe(
        docs, _drive, "doc1",
        {"op": "replace_range", "range": "target", "text": "Zulu"},
        None)
    assert out["applied_as"] == "named-range-index"
    assert out["tab_id"] is None and out["final_text"] == "Zulu"
    assert len(docs.batches) == 2  # canary, then exactly one semantic batch
    batch = docs.batches[-1]
    assert not any("replaceAllText" in req for req in batch)
    assert batch[0]["deleteContentRange"]["range"]["startIndex"] > 13
    assert batch[1]["deleteContentRange"]["range"] == {
        "startIndex": 7, "endIndex": 13,
    }
    assert batch[2]["insertText"] == {"location": {"index": 7},
                                       "text": "Zulu"}


def test_named_target_does_not_require_unique_text(engine, monkeypatch):
    docs, drive = _commented_duplicate(monkeypatch, engine)
    # The duplicate is deliberate; named ranges bypass only the quote
    # uniqueness gate, while the comment-safe path remains pinned/indexed.
    engine._apply_op_anchor_safe(
        docs, drive, "doc1",
        {"op": "replace_range", "range": "target", "text": "Zulu"},
        None)
    assert not any("replaceAllText" in req for b in docs.batches for req in b)


def test_target_live_anchor_overlap_refuses_before_semantic_batch(
        engine, monkeypatch):
    named = {"target": {"namedRanges": [{"namedRangeId": "nr-target", "ranges": [{
        "startIndex": 7, "endIndex": 13,
    }]}]}}
    doc = make_doc(["One", "Alpha", "Charlie"], named_ranges=named)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("One", []), ("Alpha", [("0", 0, 5)]),
                             ("Charlie", [])], [("0", "A", CREATED_SEC)]),
    )
    wire(engine, monkeypatch, docs, drive)
    with pytest.raises(engine.PatchOpError, match="named range overlaps"):
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_range", "range": "target", "text": "Zulu"},
            None)
    assert not any(len(b) > 1 for b in docs.batches)


def test_other_named_range_is_a_fence_but_target_is_excluded(
        engine, monkeypatch):
    named = {
        "target": {"namedRanges": [{"namedRangeId": "nr-target", "ranges": [{
            "startIndex": 7, "endIndex": 13,
        }]}]},
        "other": {"namedRanges": [{"namedRangeId": "nr-other", "ranges": [{
            "startIndex": 9, "endIndex": 11,
        }]}]},
    }
    docs, drive = _commented_duplicate(monkeypatch, engine, named=named)
    with pytest.raises(engine.PatchOpError, match="named range 'other'"):
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_range", "range": "target", "text": "Zulu"},
            None)
    assert len(docs.batches) == 2  # canary plus cleanup, no semantic write


def test_resolved_anchored_thread_fails_closed_before_canary(
        engine, monkeypatch):
    docs, drive = _commented_duplicate(
        monkeypatch, engine,
        comments=[api_comment("c1", "A", CREATED, resolved=True)])
    with pytest.raises(engine.PatchOpError, match="resolved anchored"):
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_range", "range": "target", "text": "Zulu"},
            None)
    assert docs.batches == []


def test_blocked_span_overlap_fails_closed(engine, monkeypatch):
    docs, drive = _commented_duplicate(monkeypatch, engine)
    real = engine._fresh_anchor_snapshot
    def blocked(*args, **kwargs):
        snap, reason = real(*args, **kwargs)
        snap["blocked"] = [(7, 13, "blocked suggestion span")]
        return snap, reason
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot", blocked)
    with pytest.raises(engine.PatchOpError, match="blocked suggestion span"):
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_range", "range": "target", "text": "Zulu"},
            None)


def test_final_comment_fingerprint_stale_refuses_and_cleans_canary(
        engine, monkeypatch):
    docs, drive = _commented_duplicate(monkeypatch, engine)
    original = engine._comments_fingerprint
    calls = [0]
    def stale(service, file_id):
        calls[0] += 1
        value = original(service, file_id)
        return value | {("changed",)}
    monkeypatch.setattr(engine, "_comments_fingerprint", stale)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    with pytest.raises(engine.PatchOpError, match="comments changed"):
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_range", "range": "target", "text": "Zulu"},
            None)
    assert docs.canary_text is None


def test_transport_ambiguity_uses_canary_outcome(engine, monkeypatch):
    docs, drive = _commented_duplicate(monkeypatch, engine)
    docs.merged = _duplicate_doc()
    docs.main_error = http_error(503)
    with pytest.raises(engine.PatchOpError) as exc:
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_range", "range": "target", "text": "Zulu"},
            None)
    assert exc.value.state == "not_applied"
    assert "canary intact" in str(exc.value)
    assert docs.canary_text is None


def test_named_batch_scopes_every_request_to_selected_tab(engine):
    class Recorder:
        def __init__(self):
            self.body = None
        def documents(self):
            return self
        def batchUpdate(self, documentId=None, body=None):
            self.body = body
            class R:
                def execute(self_inner):
                    return {}
            return R()
    docs = Recorder()
    engine._execute_named_range_replace(
        docs, "doc1", "t.second", 7, 13, "Zulu", "R1",
        {"start": 20, "end": 30, "tab_id": "t.second"})
    requests = docs.body["requests"]
    assert requests[0]["deleteContentRange"]["range"]["tabId"] == "t.second"
    assert requests[1]["deleteContentRange"]["range"]["tabId"] == "t.second"
    assert requests[2]["insertText"]["location"]["tabId"] == "t.second"
    assert all("replaceAllText" not in req for req in requests)


@pytest.mark.parametrize("ranges", [
    [
        {"namedRangeId": "nr-a", "ranges": [{"startIndex": 7,
                                                "endIndex": 10}]},
        {"namedRangeId": "nr-b", "ranges": [{"startIndex": 10,
                                                "endIndex": 13}]},
    ], [
        {"namedRangeId": "nr-a", "ranges": [{"startIndex": 7,
                                                "endIndex": 9}]},
        {"namedRangeId": "nr-b", "ranges": [{"startIndex": 11,
                                                "endIndex": 13}]},
    ],
])
def test_same_name_multiple_ids_refuses_before_canary(engine, ranges,
                                                       monkeypatch):
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    doc = _duplicate_doc({"target": {"namedRanges": ranges}})
    with pytest.raises(engine.PatchOpError, match="exactly one namedRange"):
        engine._resolve_op({"op": "replace_range", "range": "target",
                            "text": "Zulu"}, doc, None)


def test_missing_named_range_id_refuses_before_canary(engine, monkeypatch):
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    doc = _duplicate_doc({"target": {"namedRanges": [{"ranges": [{
        "startIndex": 7, "endIndex": 13,
    }]}]}})
    with pytest.raises(engine.PatchOpError, match="stable namedRangeId"):
        engine._resolve_op({"op": "replace_range", "range": "target",
                            "text": "Zulu"}, doc, None)


def test_fresh_named_range_identity_change_refuses(engine, monkeypatch):
    docs, drive = _commented_duplicate(monkeypatch, engine)
    original = engine._resolve_op
    calls = [0]
    def changed(op, tab, tid):
        calls[0] += 1
        result = original(op, tab, tid)
        if calls[0] == 2:
            result["named_range_id"] = "nr-replaced"
        return result
    monkeypatch.setattr(engine, "_resolve_op", changed)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    with pytest.raises(engine.PatchOpError, match="identity or range changed"):
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_range", "range": "target", "text": "Zulu"},
            None)
    assert docs.canary_text is None
