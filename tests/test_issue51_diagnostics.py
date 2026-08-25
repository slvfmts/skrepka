"""Bounded, actionable refusal evidence for issue #51."""

import pytest

from test_sync_anchors import (  # noqa: E402
    CREATED,
    CREATED_SEC,
    DocsStub,
    DriveStub,
    _docx_builder,
    api_comment,
    make_doc,
    make_workdir,
    wire,
)


def test_c1_diagnostics_report_ranges_affixes_and_real_recovery(engine):
    paths, advice = engine._c1_recovery_advice(
        "Уцелеть должен ИСХОДНЫЙ символ якоря", comment_id="c1")
    details = engine._c1_rewrite_details(
        "before old after", "before NEW after", 10, 26,
        [(17, 20, "old", "docx-1")], recovery_paths=paths,
        recovery=advice)

    assert details["anchor_range"] == [17, 20]
    assert details["edit_range"] == [10, 26]
    assert details["attempted_edit_range"] == [10, 26]
    assert details["common_prefix"] == "before "
    assert details["common_suffix"] == " after"
    assert details["common_prefix_utf16"] == 7
    assert details["common_suffix_utf16"] == 6
    assert "replace_anchor" in details["recovery"]
    assert "replace_around_anchor" not in details["recovery"]
    assert "replace_anchor" in details["recovery_paths"]
    # The suggested anchor-only route is a real, already-proven request path.
    requests = engine._exact_anchor_rewrite_requests(
        "old", "NEW", 17, 20)
    assert requests and "replaceAllText" in requests[1]


def test_structured_diagnostics_survive_patch_operation_errors(engine):
    engine._RAISE_ERRORS = True
    try:
        with pytest.raises(engine.PatchOpError) as raised:
            engine._error("bounded refusal", reason="issue51_test",
                          details={"range": [1, 2]})
    finally:
        engine._RAISE_ERRORS = False
    assert raised.value.reason == "issue51_test"
    assert raised.value.details == {"range": [1, 2]}


def test_patch_receipt_keeps_c1_reason_and_details(engine, monkeypatch,
                                                   tmp_path, capsys):
    doc = make_doc(["Alpha", "Bravo", "Charlie"])
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []), ("Bravo", [("0", 1, 4)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    ops = tmp_path / "ops.json"
    ops.write_text(
        '[{"op":"replace_quote","quote":"Bravo","with":"Zulu"}]',
        encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        engine.patch_doc("doc1", str(ops))
    assert exc.value.code == 3
    out = __import__("json").loads(capsys.readouterr().out)
    refusal = out["refused"][0]
    assert refusal["reason"] == "c1_anchor_rewrite_refused"
    assert refusal["details"]["anchor_range"] == [8, 11]
    assert refusal["details"]["edit_range"] == [7, 12]
    assert "common_prefix_utf16" in refusal["details"]
    assert "replace_anchor" in refusal["details"]["recovery_paths"]
    assert "replace_around_anchor" not in refusal["details"]["recovery_paths"]


def test_c1_diagnostics_make_control_characters_visible_and_bound_output(engine):
    details = engine._c1_rewrite_details(
        "x\v" + "s" * 200, "y\v" + "s" * 200, 1, 204,
        [(1, 4, "x\v", "docx-1")])
    assert "␋" in details["common_suffix"] or "␋" in details["common_prefix"]
    assert len(details["common_prefix"]) <= 65
    assert len(details["common_suffix"]) <= 65


def test_c1_affixes_use_utf16_width_for_emoji(engine):
    details = engine._c1_rewrite_details(
        "😀old😀", "😀new😀", 10, 20, [(12, 15, "old", "docx-1")])
    assert details["common_prefix"] == "😀"
    assert details["common_suffix"] == "😀"
    assert details["common_prefix_utf16"] == 2
    assert details["common_suffix_utf16"] == 2


@pytest.mark.parametrize(
    ("why", "expected", "forbidden"),
    [
        ("Этот фрагмент занимает несколько абзацев", "separate_paragraph_edits",
         {"replace_anchor", "replace_around_anchor"}),
        ("В новом тексте есть табуляция", "split_operations",
         {"replace_anchor", "replace_around_anchor"}),
        ("Тот же текст встречается в оглавлении", "ui",
         {"replace_anchor", "replace_around_anchor"}),
        ("На этом фрагменте стоит машинную пометку —", "release_named_range",
         {"replace_anchor", "replace_around_anchor"}),
        ("На этом же фрагменте есть ещё один комментарий", "separate_surrounding_edits",
         {"replace_anchor", "replace_around_anchor"}),
        ("Фрагмент кончается символом", "move_boundary",
         {"replace_anchor", "replace_around_anchor"}),
    ],
)
def test_c1_inapplicable_recovery_routes_are_not_advertised(
        engine, why, expected, forbidden):
    paths, _advice = engine._c1_recovery_advice(why, comment_id="c1")
    assert expected in paths
    assert not (set(paths) & forbidden)


def test_around_anchor_is_advertised_only_for_its_explicit_schema(engine):
    paths, _advice = engine._c1_recovery_advice(
        "generic refusal", comment_id="c1", operation="replace_around_anchor")
    assert paths == ["replace_around_anchor", "ui"]


def test_sync_residual_duplicate_guard_reports_bounded_evidence(
        engine, monkeypatch, tmp_path, capsys):
    base = ["P1", "P2", "DUP", "A", "DUP", "tail"]
    local = ["P2", "P1", "DUP", "A", "DUP", "DUP", "tail"]
    remote = list(base)
    doc = make_doc(base)
    docs = DocsStub(doc, merged_doc=make_doc(remote, rev="R2"))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED),
         api_comment("c2", "B", "2026-07-13T18:10:00.000Z")],
        _docx_builder(
            docs,
            [(text, [("0", 0, len(text))] if text == "P1" else
                     [("1", 0, len(text))] if text == "P2" else [])
             for text in base],
            [("0", "A", CREATED_SEC), ("1", "B", "2026-07-13T18:10:00Z")]),
        html=("".join(f"<p>{text}</p>" for text in remote)).encode())
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, "\n\n".join(base),
                        "\n\n".join(local))
    with pytest.raises(SystemExit) as exc:
        engine.sync_doc("doc1", path)
    assert exc.value.code == 1
    out = __import__("json").loads(capsys.readouterr().out)
    assert out["reason"] == "sync_duplicate_alignment_ambiguous"
    evidence = out["details"]
    assert evidence["side"] == "local"
    sample = evidence["samples"][0]
    assert sample["quote"] == "DUP"
    assert sample["base_position_count"] == 2
    assert sample["other_position_count"] == 3
    assert len(sample["base_positions"]) <= 8
    assert len(sample["other_positions"]) <= 8
    semantic_batches = [batch for batch in docs.batches
                        if len(batch) > 1 or any(
                            "insertText" in request
                            and "skrepka-canary" not in str(request)
                            for request in batch)]
    assert semantic_batches == []


def test_r15_localizes_many_duplicate_keys_without_false_blocker(
        engine, monkeypatch, tmp_path, capsys):
    keys = [f"DUP-{i:02d}-" + ("x" * 80) for i in range(12)]
    base = [value for value in keys for _ in range(2)]
    local = [value for value in keys for _ in range(3)]
    doc = make_doc(base)
    docs = DocsStub(doc, merged_doc=make_doc(base, rev="R2"))
    drive = DriveStub(
        [], _docx_builder(docs, [(text, []) for text in base], []),
        html=("".join(f"<p>{text}</p>" for text in base)).encode())
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, "\n\n".join(base),
                        "\n\n".join(local))
    with pytest.raises(SystemExit) as exc:
        engine.sync_doc("doc1", path)
    assert exc.value.code == 3
    out = __import__("json").loads(capsys.readouterr().out)
    assert out["action"] == "partial-noop"
    deferred = out["deferred"]
    assert deferred["reason"] == "duplicate-ambiguity"
    assert deferred["keys_count"] == 12
    assert len(deferred["keys"]) == 8
    assert len(deferred["keys_sha256"]) == 64


def test_public_deferred_summary_caps_long_nested_duplicate_keys(engine):
    secret = "TOP-SECRET-DUPLICATE-" + ("x" * 10_000)
    plan = {
        "reason": "duplicate-ambiguity",
        "keys": [secret] * 20,
        "nested": [[secret, {secret: secret}]],
    }
    public = engine._public_deferred_summary(plan)
    encoded = __import__("json").dumps(public, ensure_ascii=False,
                                         sort_keys=True)
    assert secret not in encoded
    assert public["keys_count"] == 20
    assert len(public["keys_sha256"]) == 64
    assert len(public["keys"][0]) <= engine._PUBLIC_SCALAR_LIMIT
    assert public == engine._public_deferred_summary(plan)

    def assert_bounded(value):
        if isinstance(value, str):
            assert len(value) <= engine._PUBLIC_SCALAR_LIMIT
        elif isinstance(value, dict):
            for key, item in value.items():
                assert len(str(key)) <= engine._PUBLIC_SCALAR_LIMIT
                assert_bounded(item)
        elif isinstance(value, list):
            for item in value:
                assert_bounded(item)

    assert_bounded(public)


def test_sync_twin_evidence_is_bounded_but_keeps_counts_and_hashes(engine):
    key = ("p", "same", ())
    base = [key] * 30
    other = [key] * 45
    witness = "sensitive twin " + ("x" * 100)
    base_elements = [{"text": witness} for _ in range(30)]
    other_elements = [{"text": witness} for _ in range(45)]
    evidence = engine._duplicate_alignment_evidence(
        base, other, {key}, base_elements, other_elements)
    sample = evidence["samples"][0]
    assert sample["base_position_count"] == 30
    assert sample["other_position_count"] == 45
    assert len(sample["base_positions"]) <= 8
    assert len(sample["other_positions"]) <= 8
    assert len(sample["quote"]) <= 65
    assert sample["quote"].endswith("…")
    assert len(sample["quote_sha256"]) == 64
    assert len(sample["base_positions_sha256"]) == 64
    assert len(sample["other_positions_sha256"]) == 64
