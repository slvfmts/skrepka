"""Adversarial contract tests for #33's explicit anchor split."""

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


def test_replace_around_anchor_schema_requires_exact_new_anchor(engine, doc_tab,
                                                                monkeypatch):
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    base = {"op": "replace_around_anchor", "comment_id": "c1",
            "quote": "pre old post", "with": "pre OLD post",
            "before_utf16": 4, "after_utf16": 5}
    with pytest.raises(engine.PatchOpError, match="anchor object"):
        engine._resolve_op(base, doc_tab([(1, 14, "pre old post\n")]), None)
    missing_extents = dict(base)
    missing_extents.pop("before_utf16")
    with pytest.raises(engine.PatchOpError, match="before_utf16"):
        engine._resolve_op(
            {**missing_extents,
             "anchor": {"text": "OLD", "start_utf16": 4}},
            doc_tab([(1, 14, "pre old post\n")]), None)
    with pytest.raises(engine.PatchOpError, match="strictly contain"):
        engine._resolve_op(
            {**base, "before_utf16": 0, "after_utf16": 0,
             "anchor": {"text": "OLD", "start_utf16": 4}},
            doc_tab([(1, 14, "pre old post\n")]), None)
    with pytest.raises(engine.PatchOpError, match="start_utf16"):
        engine._resolve_op({**base, "anchor": {"text": "OLD",
                                               "start_utf16": True}},
                           doc_tab([(1, 14, "pre old post\n")]), None)
    with pytest.raises(engine.PatchOpError, match="one paragraph"):
        engine._resolve_op({**base, "with": "pre\nOLD post",
                            "anchor": {"text": "OLD", "start_utf16": 4}},
                           doc_tab([(1, 14, "pre old post\n")]), None)
    out = engine._resolve_op(
        {**base, "anchor": {"text": "OLD", "start_utf16": 4}},
        doc_tab([(1, 14, "pre old post\n")]), None)
    assert out["kind"] == "replace_anchor"
    assert out["start"] is None and out["end"] is None


def test_split_plan_uses_explicit_utf16_fragment_and_ordered_requests(
        engine, doc_tab):
    # Old anchor is [5, 8); the emoji in the replacement occupies two units,
    # so OLD starts at UTF-16 offset 6, not Python offset 5.
    tab = doc_tab([(1, 14, "pre old post\n")])
    plan, reason = engine._replace_around_anchor_plan(
        tab,
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre 😀OLD post",
         "anchor": {"text": "OLD", "start_utf16": 6},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert reason is None
    requests, receipt = plan
    # Proven rewrite path first; the outer sides follow in a single batch.
    assert "insertText" in requests[0]
    assert "replaceAllText" in requests[1]
    assert "deleteContentRange" in requests[2]
    assert requests[3]["deleteContentRange"]["range"] == {
        "startIndex": 8, "endIndex": 13}
    assert requests[4]["insertText"]["text"] == " post"
    assert requests[5]["deleteContentRange"]["range"] == {
        "startIndex": 1, "endIndex": 5}
    assert requests[6]["insertText"]["text"] == "pre 😀"
    assert receipt["anchor_text_after_preview"] == "OLD"
    assert receipt["anchor_start_utf16"] == 6


@pytest.mark.parametrize("before, after, omitted", [(0, 5, "before"),
                                                      (4, 0, "after")])
def test_split_plan_omits_zero_width_side_requests(engine, doc_tab, before,
                                                   after, omitted):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": ("old post" if omitted == "before" else "pre old"),
         "with": ("OLD post" if omitted == "before" else "pre OLD"),
         "anchor": {"text": "OLD", "start_utf16":
                     (0 if omitted == "before" else 4)},
         "before_utf16": before, "after_utf16": after},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert reason is None
    requests, _receipt = plan
    ranges = [r["deleteContentRange"]["range"] for r in requests
              if "deleteContentRange" in r]
    if omitted == "before":
        assert {"startIndex": 1, "endIndex": 5} not in ranges
    else:
        assert {"startIndex": 8, "endIndex": 13} not in ranges


@pytest.mark.parametrize("offset", [5, 7])
def test_split_plan_rejects_utf16_surrogate_or_wrong_fragment(engine, doc_tab,
                                                              offset):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre 😀OLD post",
         "anchor": {"text": "OLD", "start_utf16": offset},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert plan is None
    assert "anchor.start_utf16" in reason


def test_utf16_partition_rejects_offsets_inside_surrogate(engine):
    assert engine._utf16_partition("😀x", 1, 2) is None


def test_split_plan_accepts_duplicate_outer_quote_at_marker_relative_range(
        engine, doc_tab):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n"),
                 (14, 27, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert plan is not None
    assert reason is None


def test_split_plan_requires_quote_to_witness_relative_range(engine, doc_tab):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "wrong outer", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert plan is None
    assert "paragraph" in reason


def test_split_plan_requires_old_marker_text_to_match_indices(engine, doc_tab):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        6, 9, [(6, 9, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert plan is None
    assert "boundaries" in reason or "paragraph" in reason


def test_split_plan_refuses_foreign_fence_before_requests(engine, doc_tab):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0"), (10, 12, "po", "foreign")],
        [], {"docx-0": "c1", "foreign": "c2"}, [])
    assert plan is None
    assert "another live comment" in reason


def test_split_plan_refuses_foreign_anchor_on_same_range(engine, doc_tab):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0"), (5, 8, "old", "foreign")],
        [], {"docx-0": "c1", "foreign": "c2"}, [])
    assert plan is None
    assert "another live comment" in reason


def test_split_plan_rejects_named_fence(engine, doc_tab):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"},
        [(1, 3, "named range 'mark'")])
    assert plan is None
    assert "named range" in reason


def test_split_plan_rejects_protected_fence(engine, doc_tab):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")],
        [(1, 4, "protected fence")], {"docx-0": "c1"}, [])
    assert plan is None
    assert "protected fence" in reason


def test_split_plan_rejects_suggestion_fence(engine, doc_tab):
    tab = doc_tab([(1, 14, "pre old post\n",
                    {"suggestedDeletionIds": {"s1": {}}})])
    plan, reason = engine._replace_around_anchor_plan(
        tab,
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert plan is None
    assert "suggestion" in reason


def test_split_plan_rejects_mixed_style_outer_range(engine, doc_tab):
    tab = doc_tab([(1, 5, "pre ", {"textStyle": {"bold": True}}),
                   (5, 14, "old post\n", {"textStyle": {}})])
    plan, reason = engine._replace_around_anchor_plan(
        tab,
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert plan is None
    assert "mixed text styles" in reason


def test_split_plan_rejects_malformed_relative_marker_boundary(engine, doc_tab):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 15, "pre 😀old\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre 😀old", "with": "pre 😀OLD",
         "anchor": {"text": "OLD", "start_utf16": 6},
         "before_utf16": 1, "after_utf16": 4},
        7, 10, [(7, 10, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert plan is None
    assert "outer replacement" in reason or "UTF-16 boundaries" in reason


@pytest.mark.parametrize("old, start, end", [("x", 5, 6), ("😀", 5, 7)])
def test_split_plan_refuses_one_codepoint_old_anchor(engine, doc_tab, old,
                                                     start, end):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 20, f"pre {old} post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": f"pre {old} post", "with": "pre NEW post",
         "anchor": {"text": "NEW", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        start, end, [(start, end, old, "docx-0")], [],
        {"docx-0": "c1"}, [])
    assert plan is None
    assert "one-codepoint" in reason


@pytest.mark.parametrize("old, start, end", [("x", 5, 6), ("😀", 5, 7)])
def test_split_plan_refuses_one_codepoint_true_noop_before_shortcut(
        engine, doc_tab, old, start, end):
    """A textual no-op must not bypass the unproven one-codepoint protocol."""
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 20, f"pre {old} post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": f"pre {old} post", "with": f"pre {old} post",
         "anchor": {"text": old, "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        start, end, [(start, end, old, "docx-0")], [],
        {"docx-0": "c1"}, [])
    assert plan is None
    assert "one-codepoint" in reason


def test_split_plan_noop_returns_empty_semantic_plan(engine, doc_tab):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre old post",
         "anchor": {"text": "old", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert reason is None
    requests, receipt = plan
    assert requests == []
    assert receipt["no_op"] is True


def test_split_plan_noop_rejects_duplicate_new_anchor_offset(engine, doc_tab):
    """Equal text does not authorize moving the thread to another duplicate."""
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 18, "pre old old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old old post", "with": "pre old old post",
         # The fresh marker is the first ``old`` (relative offset 4); 8 is
         # the second occurrence and must be a bounded refusal, not a no-op.
         "anchor": {"text": "old", "start_utf16": 8},
         "before_utf16": 4, "after_utf16": 9},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert plan is None
    assert "no-op anchor offset" in reason


@pytest.mark.parametrize("fence", [
    [(10, 12, "foreign")],
    [(1, 3, "named range 'mark'")],
])
def test_split_plan_true_noop_bypasses_deletion_only_fences(
        engine, doc_tab, fence):
    blocked, anchors, named = [], [], []
    if fence[0][2] == "foreign":
        anchors = [(5, 8, "old", "docx-0"), (10, 12, "ol", "foreign")]
    else:
        named = fence
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre old post",
         "anchor": {"text": "old", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, anchors or [(5, 8, "old", "docx-0")], blocked,
        {"docx-0": "c1", "foreign": "c2"}, named)
    assert reason is None
    assert plan[0] == [] and plan[1]["no_op"] is True


def test_split_plan_refuses_exact_remaining_protected_fence_on_noop(
        engine, doc_tab):
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre old post",
         "anchor": {"text": "old", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")],
        [(5, 8, "ambiguous anchors c1/c2")], {"docx-0": "c1"}, [])
    assert plan is None
    assert "protected fence" in reason


def test_split_plan_same_thread_root_reply_owner_lifting_succeeds(
        engine, doc_tab):
    # The caller has structurally proved both descriptors belong to c1 and
    # removed the exact owned fence before entering the planner.
    plan, reason = engine._replace_around_anchor_plan(
        doc_tab([(1, 14, "pre old post\n")]),
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "root"), (5, 8, "old", "reply")], [],
        {"root": "c1", "reply": "c1"}, [])
    assert reason is None
    assert plan is not None


def test_split_plan_noop_still_refuses_suggestion_fence(engine, doc_tab):
    tab = doc_tab([(1, 14, "pre old post\n",
                    {"suggestedDeletionIds": {"s1": {}}})])
    plan, reason = engine._replace_around_anchor_plan(
        tab,
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre old post",
         "anchor": {"text": "old", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert plan is None
    assert "suggestion" in reason


def test_split_plan_true_noop_bypasses_mixed_style_fence(engine, doc_tab):
    tab = doc_tab([(1, 5, "pre ", {"textStyle": {"bold": True}}),
                   (5, 14, "old post\n", {"textStyle": {}})])
    plan, reason = engine._replace_around_anchor_plan(
        tab,
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre old post",
         "anchor": {"text": "old", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5},
        5, 8, [(5, 8, "old", "docx-0")], [], {"docx-0": "c1"}, [])
    assert reason is None
    assert plan[0] == [] and plan[1]["no_op"] is True


def test_duplicate_outer_lines_use_the_selected_marker_line(
        engine, monkeypatch):
    docs = DocsStub(make_doc(["pre old post", "pre old post"]))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("pre old post", []),
                             ("pre old post", [("0", 4, 7)])],
                      [("0", "A", CREATED_SEC)]),
    )
    wire(engine, monkeypatch, docs, drive)
    snapshot = {
        "anchors": [(18, 21, "old", "0")], "ambiguous": [],
        "docx_outline": [{"kind": "p", "text": "pre old post"},
                         {"kind": "p", "text": "pre old post"}],
        "segment_first": 0, "segment_last": None, "blocked": [],
        "attribution": {"0": "c1"}, "fp1": set(), "canary": {
            "text": "canary", "start": 30, "end": 36, "tab_id": None},
        "r1": "R1",
    }
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot",
                        lambda *args, **kwargs: (snapshot, None))
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *args: True)
    monkeypatch.setattr(engine, "_comments_fingerprint", lambda *args: set())
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    out = engine._apply_op_anchor_safe(
        docs, drive, "doc1",
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5}, None)
    assert out["applied_as"] == "around-anchor"
    semantic = docs.batches[-1]
    assert any(r.get("replaceAllText", {}).get("containsText", {})
               .get("text", "").startswith("ol") for r in semantic)
    assert any(r.get("deleteContentRange", {}).get("range") == {
        "startIndex": 14, "endIndex": 18} for r in semantic)


def test_duplicate_same_thread_root_reply_lifts_fence_and_rewrites_selected(
        engine, monkeypatch):
    docs = DocsStub(make_doc(["pre old post", "pre old post"]))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("pre old post", []),
                             ("pre old post", [("root", 4, 7)])],
                      [("root", "A", CREATED_SEC)]),
    )
    wire(engine, monkeypatch, docs, drive)
    snapshot = {
        "anchors": [],
        "ambiguous": [{
            "docx_id": "root", "top": 1, "end_top": 1,
            "start_off": 4, "end_off": 7,
            "candidates": [(1, 14), (14, 27)],
            "para_text": "pre old post",
        }],
        "docx_outline": [{"kind": "p", "text": "pre old post"},
                         {"kind": "p", "text": "pre old post"}],
        "segment_first": 0, "segment_last": None,
        "blocked": [(18, 21, "ambiguous c1 root/reply fence")],
        "attribution": {"root": "c1"}, "fp1": set(),
        "canary": {"text": "canary", "start": 30, "end": 36,
                    "tab_id": None},
        "r1": "R1",
    }
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot",
                        lambda *args, **kwargs: (snapshot, None))
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *args: True)
    monkeypatch.setattr(engine, "_comments_fingerprint", lambda *args: set())
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    out = engine._apply_op_anchor_safe(
        docs, drive, "doc1",
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5}, None)
    assert out["applied_as"] == "around-anchor"
    assert any("replaceAllText" in request for request in docs.batches[-1])


def test_replace_around_anchor_noop_cleans_canary_without_semantic_write(
        engine, monkeypatch):
    docs = DocsStub(make_doc(["pre old post"]))
    drive = DriveStub([api_comment("c1", "A", CREATED)],
                      _docx_builder(docs, [("pre old post", [("0", 4, 7)])],
                                    [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    snapshot = {
        "anchors": [(5, 8, "old", "0")], "ambiguous": [], "blocked": [],
        "attribution": {"0": "c1"}, "fp1": set(), "canary": {
            "text": "canary", "start": 20, "end": 26, "tab_id": None},
        "r1": "R1", "docx_outline": [{"kind": "p"}],
        "segment_first": 0, "segment_last": None,
    }
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot",
                        lambda *args, **kwargs: (snapshot, None))
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *args: True)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    out = engine._apply_op_anchor_safe(
        docs, drive, "doc1",
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre old post",
         "anchor": {"text": "old", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5}, None)
    assert out["applied_as"] == "no-op"
    assert docs.batches == []


def test_replace_around_anchor_noop_cleanup_failure_is_recoverable(
        engine, monkeypatch):
    docs = DocsStub(make_doc(["pre old post"]))
    drive = DriveStub([api_comment("c1", "A", CREATED)],
                      _docx_builder(docs, [("pre old post", [("0", 4, 7)])],
                                    [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    canary = "__SKREPKA_CANARY_33__"
    snapshot = {
        "anchors": [(5, 8, "old", "0")], "ambiguous": [], "blocked": [],
        "attribution": {"0": "c1"}, "fp1": set(),
        "canary": {"text": canary, "start": 20, "end": 20 + len(canary),
                    "tab_id": None},
        "r1": "R1", "docx_outline": [{"kind": "p"}],
        "segment_first": 0, "segment_last": None,
    }
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot",
                        lambda *args, **kwargs: (snapshot, None))
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *args: False)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    with pytest.raises(engine.PatchOpError) as exc:
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_around_anchor", "comment_id": "c1",
             "quote": "pre old post", "with": "pre old post",
             "anchor": {"text": "old", "start_utf16": 4},
             "before_utf16": 4, "after_utf16": 5}, None)
    message = str(exc.value)
    assert canary in message
    assert "manually" in message
    assert "semantic edit was not sent" in message
    assert docs.batches == []


@pytest.mark.parametrize("fence", ["foreign", "named"])
def test_apply_noop_bypasses_deletion_only_fence_without_batch(
        engine, monkeypatch, fence):
    docs = DocsStub(make_doc(["pre old post"]))
    drive = DriveStub([api_comment("c1", "A", CREATED)],
                      _docx_builder(docs, [("pre old post", [("0", 4, 7)])],
                                    [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    snapshot = {
        "anchors": [(5, 8, "old", "0")], "ambiguous": [],
        "blocked": [],
        "attribution": {"0": "c1"}, "fp1": set(),
        "canary": {"text": "canary", "start": 20, "end": 26,
                    "tab_id": None},
        "r1": "R1", "docx_outline": [{"kind": "p"}],
        "segment_first": 0, "segment_last": None,
    }
    if fence == "foreign":
        snapshot["anchors"].append((10, 12, "ol", "foreign"))
        snapshot["attribution"]["foreign"] = "c2"
    if fence == "named":
        monkeypatch.setattr(engine, "_named_range_intervals",
                            lambda *args, **kwargs:
                            [(1, 3, "named range 'mark'")])
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot",
                        lambda *args, **kwargs: (snapshot, None))
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *args: True)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    out = engine._apply_op_anchor_safe(
        docs, drive, "doc1",
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre old post",
         "anchor": {"text": "old", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5}, None)
    assert out["applied_as"] == "no-op"
    assert docs.batches == []


def test_apply_ambiguous_same_range_refuses_before_semantic_batch(
        engine, monkeypatch):
    docs = DocsStub(make_doc(["pre old post"]))
    drive = DriveStub([api_comment("c1", "A", CREATED)],
                      _docx_builder(docs, [("pre old post", [("0", 4, 7)])],
                                    [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    snapshot = {
        # Both descriptors cover the same range, but only c1 is requested.
        "anchors": [(5, 8, "old", "root"), (5, 8, "old", "foreign")],
        "ambiguous": [{"docx_id": "root"}, {"docx_id": "foreign"}],
        "blocked": [(5, 8, "ambiguous anchors c1/c2")],
        "attribution": {"root": "c1", "foreign": "c2"}, "fp1": set(),
        "canary": {"text": "canary", "start": 20, "end": 26,
                    "tab_id": None},
        "r1": "R1", "docx_outline": [{"kind": "p"}],
        "segment_first": 0, "segment_last": None,
    }
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot",
                        lambda *args, **kwargs: (snapshot, None))
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *args: True)
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    with pytest.raises(engine.PatchOpError, match="protected fence"):
        engine._apply_op_anchor_safe(
            docs, drive, "doc1",
            {"op": "replace_around_anchor", "comment_id": "c1",
             "quote": "pre old post", "with": "pre OLD post",
             "anchor": {"text": "OLD", "start_utf16": 4},
             "before_utf16": 4, "after_utf16": 5}, None)
    assert docs.batches == []


def test_replace_around_anchor_uses_fresh_marker_and_bounded_receipt(
        engine, monkeypatch):
    docs = DocsStub(make_doc(["pre old post", "tail"]))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("pre old post", [("0", 4, 7)]),
                             ("tail", [])], [("0", "A", CREATED_SEC)]),
    )
    wire(engine, monkeypatch, docs, drive)
    snapshot = {
        "anchors": [(5, 8, "old", "0")], "ambiguous": [],
        "docx_outline": [{"kind": "p"}, {"kind": "p"}],
        "segment_first": 0, "segment_last": None, "blocked": [],
        "attribution": {"0": "c1"}, "fp1": set(), "canary": {
            "text": "canary", "start": 20, "end": 26, "tab_id": None},
        "r1": "R1",
    }
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot",
                        lambda *args, **kwargs: (snapshot, None))
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *args: True)
    monkeypatch.setattr(engine, "_comments_fingerprint", lambda *args: set())
    monkeypatch.setattr(engine, "_RAISE_ERRORS", True)
    out = engine._apply_op_anchor_safe(
        docs, drive, "doc1",
        {"op": "replace_around_anchor", "comment_id": "c1",
         "quote": "pre old post", "with": "pre OLD post",
         "anchor": {"text": "OLD", "start_utf16": 4},
         "before_utf16": 4, "after_utf16": 5}, None)
    assert out["applied_as"] == "around-anchor"
    assert out["anchor_text_after_preview"] == "OLD"
    semantic = docs.batches[-1]
    # Canary cleanup is first; the semantic sequence starts with the proven
    # anchor rewrite and only then touches the two outside pieces.
    assert semantic[0].get("deleteContentRange")
    semantic = [r for r in semantic if not r.get("deleteContentRange", {})
                .get("range", {}).get("startIndex") == 20]
    assert "replaceAllText" in semantic[1]
    assert sum("replaceAllText" in r for r in semantic) == 1
