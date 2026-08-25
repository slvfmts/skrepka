"""Adversarial read-only advisory contract for issue #50."""

import json

import pytest


def _tab(text="Alpha"):
    return {"body": {"content": [{
        "startIndex": 1, "endIndex": 1 + len(text),
        "paragraph": {"elements": [{
            "startIndex": 1, "endIndex": 1 + len(text),
            "textRun": {"content": text},
        }]},
    }]}}


def _doc(text="Alpha", **extra):
    out = {"revisionId": "R0", **_tab(text)}
    out.update(extra)
    return out


def _plan(engine, ops, *, anchored=False, text="Alpha"):
    return engine.compile_index_plan(engine.prepare_patch(
        _doc(text), ops, anchored=anchored))


def test_clean_replace_is_would_apply(engine):
    assert _plan(engine, [{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}])[0]["status"] == "would_apply"


def test_clean_noop_is_noop(engine):
    assert _plan(engine, [{"op": "replace_quote", "quote": "Alpha", "with": "Alpha"}])[0]["status"] == "noop"


def test_clean_unknown_quote_is_would_refuse(engine):
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Missing", "with": "Beta"}])[0]
    assert verdict["status"] == "would_refuse"


def test_clean_schema_is_would_refuse(engine):
    verdict = _plan(engine, [{"op": "wat", "quote": "Alpha", "with": "Beta"}])[0]
    assert verdict["status"] == "would_refuse"


def test_clean_suggestion_is_would_refuse(engine):
    doc = _doc("Alpha")
    doc["body"]["content"][0]["paragraph"]["elements"][0]["textRun"]["suggestedInsertionIds"] = ["s1"]
    prepared = engine.prepare_patch(doc, [{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}])
    verdict = engine.compile_index_plan(prepared)[0]
    assert verdict["status"] == "would_refuse" and verdict["reason"] == "suggestion_gate"


def test_clean_toc_duplicate_is_would_refuse(engine):
    tab = _tab("Alpha")
    tab["body"]["content"].append({"startIndex": 7, "endIndex": 12,
        "tableOfContents": {"content": [{"startIndex": 7, "endIndex": 12,
            "paragraph": {"elements": [{"startIndex": 7, "endIndex": 12,
                "textRun": {"content": "Alpha"}}]}}]}})
    verdict = engine.compile_index_plan(engine.prepare_patch(
        {"revisionId": "R0", **tab}, [{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}]))[0]
    assert verdict["status"] == "would_refuse"


def test_clean_style_is_would_apply(engine):
    assert _plan(engine, [{"op": "style_quote", "quote": "Alpha", "style": {"bold": True}}])[0]["status"] == "would_apply"


def test_clean_insert_is_would_apply(engine):
    assert _plan(engine, [{"op": "insert_after_quote", "quote": "Alpha", "text": "!"}])[0]["status"] == "would_apply"


def test_commented_destructive_replace_is_unknown(engine):
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}], anchored=True)[0]
    assert verdict["status"] == "unknown"
    assert verdict["reason"] == "fresh_anchor_map_requires_canary"


def test_commented_anchor_replace_is_unknown(engine):
    verdict = _plan(engine, [{"op": "replace_anchor", "comment_id": "c1", "with": "Beta"}], anchored=True)[0]
    assert verdict["status"] == "unknown"
    assert verdict["reason"] == "fresh_anchor_map_requires_canary"


def test_commented_pure_insertion_is_would_apply(engine):
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Alpha", "with": "Alpha!"}], anchored=True)[0]
    assert verdict["status"] == "would_apply"


def test_commented_style_is_would_apply(engine):
    verdict = _plan(engine, [{"op": "style_quote", "quote": "Alpha", "style": {"bold": True}}], anchored=True)[0]
    assert verdict["status"] == "would_apply"


@pytest.mark.parametrize("occurrence", [1, 2])
def test_commented_style_explicit_occurrence_is_refused(engine, occurrence):
    text = "Alpha Alpha" if occurrence == 2 else "Alpha"
    verdict = _plan(engine, [{"op": "style_quote", "quote": "Alpha",
                              "occurrence": occurrence,
                              "style": {"bold": True}}],
                    anchored=True, text=text)[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "occurrence_not_supported_on_commented_doc"


def test_commented_style_invalid_occurrence_is_schema_refusal(engine):
    verdict = _plan(engine, [{"op": "style_quote", "quote": "Alpha",
                              "occurrence": 0,
                              "style": {"bold": True}}], anchored=True)[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "schema_invalid"


def test_clean_style_explicit_occurrence_remains_decidable(engine):
    verdict = _plan(engine, [{"op": "style_quote", "quote": "Alpha",
                              "occurrence": 2,
                              "style": {"bold": True}}], text="Alpha Alpha")[0]
    assert verdict["status"] == "would_apply"


def test_anchor_operation_without_comments_refuses(engine):
    verdict = _plan(engine, [{"op": "replace_anchor", "comment_id": "c1", "with": "Beta"}])[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "anchored_thread_required"


@pytest.mark.parametrize("occurrence", [True, False])
def test_bool_occurrence_is_schema_refusal_before_freshness(engine, occurrence):
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Alpha",
                              "with": "Beta", "occurrence": occurrence}],
                    anchored=True)[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "schema_invalid"


@pytest.mark.parametrize("occurrence", [True, False])
def test_normal_resolver_rejects_bool_occurrence(engine, occurrence):
    engine._RAISE_ERRORS = True
    try:
        with pytest.raises(engine.PatchOpError):
            engine._resolve_op({"op": "replace_quote", "quote": "Alpha",
                                "with": "Beta", "occurrence": occurrence},
                               _tab(), None)
    finally:
        engine._RAISE_ERRORS = False


def test_empty_replace_anchor_is_static_refusal_not_unknown(engine):
    verdict = _plan(engine, [{"op": "replace_anchor", "comment_id": "c1",
                              "with": ""}], anchored=True)[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "schema_invalid"


@pytest.mark.parametrize("op", [
    {"op": "replace_around_anchor", "comment_id": "c1", "quote": "x",
     "with": "", "before_utf16": 1, "after_utf16": 0,
     "anchor": {"text": "x", "start_utf16": 0}},
    {"op": "replace_around_anchor", "comment_id": "c1", "quote": "x",
     "with": "new", "before_utf16": 1, "after_utf16": 0,
     "anchor": {"text": "x", "start_utf16": 3}},
    {"op": "replace_around_anchor", "comment_id": "c1", "quote": "",
     "with": "x", "before_utf16": 1, "after_utf16": 0,
     "anchor": {"text": "x", "start_utf16": 0}},
    {"op": "replace_around_anchor", "comment_id": "c1", "quote": "😀",
     "with": "😀😀", "before_utf16": 1, "after_utf16": 0,
     "anchor": {"text": "😀", "start_utf16": 1}},
])
def test_invalid_around_anchor_is_static_refusal_not_unknown(engine, op):
    verdict = _plan(engine, [op], anchored=True)[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "schema_invalid"


def test_later_operation_after_unknown_is_not_simulated(engine):
    verdicts = _plan(engine, [
        {"op": "replace_quote", "quote": "Alpha", "with": "Beta"},
        {"op": "insert_after_quote", "quote": "Gamma", "text": "!"},
    ], anchored=True, text="Alpha Beta Gamma")
    assert verdicts[0]["status"] == "unknown"
    assert verdicts[1]["status"] == "not_simulated"
    assert verdicts[1]["depends_on"] == [0]


def test_clean_n_to_n_plus_one_uniqueness_is_not_simulated(engine):
    text = "Alpha B"
    verdicts = _plan(engine, [
        {"op": "replace_quote", "quote": "Alpha", "with": "B B"},
        {"op": "replace_quote", "quote": "B", "with": "C"},
    ], text=text)
    assert verdicts[0]["status"] == "would_apply"
    assert verdicts[1]["status"] == "not_simulated"
    assert verdicts[1]["depends_on"] == [0]


def test_clean_disjoint_multi_op_is_atomic_advisory(engine):
    verdicts = _plan(engine, [
        {"op": "replace_quote", "quote": "Alpha", "with": "A"},
        {"op": "insert_after_quote", "quote": "Alpha", "text": "!"},
    ])
    # The second op touches the first target boundary, so the overlap gate is
    # conservative and refuses both instead of guessing batch ordering.
    assert {v["status"] for v in verdicts} == {"would_refuse"}


def _ranges(engine, *spans):
    return {i: {"affect_start": start, "affect_end": end,
                "source": f"r{i}"}
            for i, (start, end) in enumerate(spans)}


def test_overlap_wide_range_conflicts_with_nonadjacent_inner_ranges(engine):
    conflicts = engine._ops_overlap_conflicts(
        _ranges(engine, (1, 7), (2, 3), (8, 9), (4, 5)))
    assert set(conflicts) == {0, 1, 3}


def test_overlap_nested_ranges_conflict_both_members(engine):
    conflicts = engine._ops_overlap_conflicts(_ranges(engine, (1, 10), (3, 4)))
    assert set(conflicts) == {0, 1}


def test_overlap_identical_ranges_conflict_both_members(engine):
    conflicts = engine._ops_overlap_conflicts(_ranges(engine, (2, 5), (2, 5)))
    assert set(conflicts) == {0, 1}


def test_overlap_boundary_touch_insert_conflicts_with_range(engine):
    conflicts = engine._ops_overlap_conflicts(_ranges(engine, (1, 4), (4, 4)))
    assert set(conflicts) == {0, 1}


def test_non_overlapping_ranges_remain_decidable(engine):
    conflicts = engine._ops_overlap_conflicts(_ranges(engine, (1, 3), (4, 4)))
    assert conflicts == {}


def test_multi_tab_selection_is_read_only_refusal(engine):
    doc = {"revisionId": "R0", "tabs": [
        {"tabProperties": {"tabId": "t1", "title": "one"}, "documentTab": _tab()},
        {"tabProperties": {"tabId": "t2", "title": "two"}, "documentTab": _tab()},
    ]}
    verdict = engine.compile_index_plan(engine.prepare_patch(
        doc, [{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}]))[0]
    assert verdict["status"] == "would_refuse" and verdict["reason"] == "tab_selection"


def test_explicit_tab_is_decidable(engine):
    doc = {"revisionId": "R0", "tabs": [
        {"tabProperties": {"tabId": "t1", "title": "one"}, "documentTab": _tab()},
        {"tabProperties": {"tabId": "t2", "title": "two"}, "documentTab": _tab()},
    ]}
    verdict = engine.compile_index_plan(engine.prepare_patch(
        doc, [{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}], tab_id="t2"))[0]
    assert verdict["status"] == "would_apply"


def test_duplicate_explicit_tab_id_refuses_with_bounded_identity(engine):
    doc = {"revisionId": "R0", "tabs": [
        {"tabProperties": {"tabId": "dup", "title": "first"}, "documentTab": _tab()},
        {"tabProperties": {"tabId": "dup", "title": "second"}, "documentTab": _tab()},
    ]}
    tid, selected, error = engine._dry_run_select_tab(doc, tab_id="dup")
    assert tid is None and selected is None
    assert error["reason"] == "tab_selection"
    assert error["details"]["code"] == "duplicate_tab_id"
    assert error["details"]["count"] == 2
    assert [c["title"] for c in error["details"]["candidates"]] == ["first", "second"]


def test_duplicate_explicit_tab_id_refuses_every_op_before_planning(engine):
    doc = {"revisionId": "R0", "tabs": [
        {"tabProperties": {"tabId": "dup", "title": "same"}, "documentTab": _tab()},
        {"tabProperties": {"tabId": "dup", "title": "same"}, "documentTab": _tab()},
    ]}
    verdicts = engine.compile_index_plan(engine.prepare_patch(
        doc, [{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}],
        tab_id="dup"))
    assert verdicts[0]["status"] == "would_refuse"
    assert verdicts[0]["reason"] == "tab_selection"
    assert verdicts[0]["details"]["code"] == "duplicate_tab_id"


def test_explicit_missing_tab_id_keeps_not_found_semantics(engine):
    doc = {"revisionId": "R0", "tabs": [
        {"tabProperties": {"tabId": "t1", "title": "one"}, "documentTab": _tab()},
    ]}
    _tid, _selected, error = engine._dry_run_select_tab(doc, tab_id="missing")
    assert isinstance(error, str) and "tab not found" in error


def test_receipt_has_zero_writes_and_safe_action(engine):
    prepared = engine.prepare_patch(_doc(), [{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}])
    receipt = engine._dry_receipt("d1", prepared, engine.compile_index_plan(prepared), revision_id="R0")
    assert receipt["action"] == "dry-run"
    assert receipt["writes_performed"] == 0
    assert "applied" not in receipt and "patched" not in receipt


def test_receipt_operations_never_claim_apply_wording(engine):
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}])[0]
    assert verdict["status"] == "would_apply"
    assert "applied" not in verdict


def test_dry_run_entrypoint_does_not_call_writer(engine, monkeypatch, tmp_path, capsys):
    class Tripwire:
        def documents(self):
            return self

        def get(self, **_):
            return type("R", (), {"execute": lambda self: _doc()})()

        def batchUpdate(self, **_):
            raise AssertionError("dry-run called batchUpdate")

    service = Tripwire()
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: service)
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments", lambda *_: ([], [], "fp", {}))
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}]))
    engine.dry_run_patch("d1", str(ops))
    out = json.loads(capsys.readouterr().out)
    assert out["writes_performed"] == 0 and out["operations"][0]["status"] == "would_apply"


def test_duplicate_tab_entrypoint_has_zero_writer_calls(engine, monkeypatch,
                                                        tmp_path, capsys):
    class Tripwire:
        def __init__(self):
            self.writes = 0

        def documents(self): return self
        def get(self, **_):
            doc = {"revisionId": "R0", "tabs": [
                {"tabProperties": {"tabId": "dup", "title": "first"}, "documentTab": _tab()},
                {"tabProperties": {"tabId": "dup", "title": "second"}, "documentTab": _tab()},
            ]}
            return type("R", (), {"execute": lambda self: doc})()
        def batchUpdate(self, **_):
            self.writes += 1
            raise AssertionError("duplicate-tab dry-run called batchUpdate")

    service = Tripwire()
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: service)
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments", lambda *_: ([], [], "fp", {}))
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}]))
    with pytest.raises(SystemExit) as exc:
        engine.dry_run_patch("d1", str(ops), tab_id="dup")
    assert exc.value.code == 3 and service.writes == 0
    out = json.loads(capsys.readouterr().out)
    assert out["operations"][0]["details"]["code"] == "duplicate_tab_id"


def test_commented_style_occurrence_cli_exit_three_zero_writer(engine, monkeypatch,
                                                               tmp_path, capsys):
    class Tripwire:
        def __init__(self): self.writes = 0
        def documents(self): return self
        def get(self, **_): return type("R", (), {"execute": lambda self: _doc()})()
        def batchUpdate(self, **_):
            self.writes += 1
            raise AssertionError("commented style dry-run called batchUpdate")

    service = Tripwire()
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: service)
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments",
                        lambda *_: ([{"id": "c1"}], [{"id": "c1"}], "fp", {}))
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([{"op": "style_quote", "quote": "Alpha",
                                "occurrence": 1, "style": {"bold": True}}]))
    with pytest.raises(SystemExit) as exc:
        engine.dry_run_patch("d1", str(ops))
    assert exc.value.code == 3 and service.writes == 0
    out = json.loads(capsys.readouterr().out)
    assert out["operations"][0]["reason"] == "occurrence_not_supported_on_commented_doc"


def test_dry_run_malformed_ops_is_exit_three_and_receipted(engine, tmp_path, capsys):
    ops = tmp_path / "ops.json"
    ops.write_text("not-json")
    with pytest.raises(SystemExit) as exc:
        engine.dry_run_patch("d1", str(ops))
    assert exc.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "dry-run" and out["writes_performed"] == 0
    assert out["operations"][0]["reason"] == "schema_invalid"


def test_dry_run_output_file_is_receipt(engine, monkeypatch, tmp_path, capsys):
    class Docs:
        def documents(self): return self
        def get(self, **_): return type("R", (), {"execute": lambda self: _doc()})()
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: Docs())
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments", lambda *_: ([], [], "fp", {}))
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}]))
    output = tmp_path / "receipt.json"
    engine.dry_run_patch("d1", str(ops), output=str(output))
    assert json.loads(output.read_text())["action"] == "dry-run"
    assert json.loads(capsys.readouterr().out)["written"] == str(output)


def test_dry_run_unknown_exit_is_three(engine, monkeypatch, tmp_path, capsys):
    class Docs:
        def documents(self): return self
        def get(self, **_): return type("R", (), {"execute": lambda self: _doc()})()
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: Docs())
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments", lambda *_: ([{"id": "c1"}], [{"id": "c1"}], "fp", {}))
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}]))
    with pytest.raises(SystemExit) as exc:
        engine.dry_run_patch("d1", str(ops))
    assert exc.value.code == 3
    assert json.loads(capsys.readouterr().out)["operations"][0]["status"] == "unknown"


def test_dry_run_does_not_call_export_or_canary_helpers(engine, monkeypatch, tmp_path):
    for name in ("_fresh_anchor_snapshot", "_cleanup_canary", "_execute_replace_all",
                 "_execute_exact_anchor_rewrite", "_apply_op_anchor_safe"):
        monkeypatch.setattr(engine, name, lambda *a, _name=name, **k: (_ for _ in ()).throw(
            AssertionError(f"dry-run called {_name}")))
    class Docs:
        def documents(self): return self
        def get(self, **_): return type("R", (), {"execute": lambda self: _doc()})()
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: Docs())
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments", lambda *_: ([], [], "fp", {}))
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}]))
    engine.dry_run_patch("d1", str(ops))


@pytest.mark.parametrize("bad", [None, {}, {"op": "replace_quote"},
                                  {"op": "insert_after_quote", "quote": "Alpha"},
                                  {"op": "replace_quote", "quote": "Alpha", "with": 3}])
def test_schema_advisory_never_writes(engine, bad):
    verdict = _plan(engine, [bad])[0]
    assert verdict["status"] in {"would_refuse", "noop"}
