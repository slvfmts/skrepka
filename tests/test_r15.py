"""Adversarial specification for r15 pinned alignment and partial sync.

These tests intentionally exercise the choices that ``difflib`` and a
component filter are most likely to get subtly wrong.  They are allowed to be
red while r15 is being implemented; unlike the older characterization suite,
they describe the new contract rather than the 0.16 behaviour.
"""

import json
from pathlib import Path

import pytest

from test_reorder import make_doc_runs, replay, run_sync, style_batch
from test_sync_anchors import (
    CREATED,
    CREATED_SEC,
    DocsStub,
    DriveStub,
    _crossing_docx,
    _docx_builder,
    _no_content_mutation,
    api_comment,
    make_doc,
    make_workdir,
    wire,
)


A = "Alpha"
B = "Bravo"
X = "Повтор"
P = "PIN — живой комментарий"
P1 = "PIN один"
P2 = "PIN два"


def _md(texts):
    return "\n\n".join(texts)


def _html(texts):
    return "".join(f"<p>{t}</p>" for t in texts).encode()


def _all_strings(value):
    """Flatten a result so assertions do not freeze incidental JSON shape."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _all_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_strings(item)
    elif value is not None:
        yield str(value)


def _partial_call(engine, capsys, file_id, md_path):
    """A partial result is actionable failure, like partial ``patch``."""
    with pytest.raises(SystemExit) as exc:
        engine.sync_doc(file_id, md_path)
    assert exc.value.code == 3
    return json.loads(capsys.readouterr().out)


def _main_text_requests(docs, original_doc):
    """Return R0-coordinate text requests, omitting the canary delete.

    A commented sync sends the canary deletion as request zero in the same
    atomic batch.  ``replay`` models the original R0 body, so that one request
    must be stripped before replaying the semantic plan.
    """
    body_end = original_doc["body"]["content"][-1]["endIndex"]
    for batch in docs.batches:
        semantic_insert = any(
            "insertText" in req
            and "skrepka-canary" not in req["insertText"].get("text", "")
            for req in batch
        )
        if not semantic_insert:
            continue
        out = []
        for req in batch:
            rng = (req.get("deleteContentRange") or {}).get("range")
            if rng and rng.get("endIndex", 0) > body_end:
                continue  # canary delete; every semantic range is in R0
            out.append(req)
        return out
    raise AssertionError("semantic text batch was not sent")


def _assert_no_delete_of(doc, texts, protected_positions, requests):
    content = [el for el in doc["body"]["content"] if "paragraph" in el]
    protected = [
        (content[i]["startIndex"], content[i]["startIndex"] + len(texts[i]))
        for i in protected_positions
    ]
    for req in requests:
        rng = (req.get("deleteContentRange") or {}).get("range")
        if not rng:
            continue
        for start, end in protected:
            assert not (rng["startIndex"] < end and start < rng["endIndex"]), (
                f"delete {rng} intersects protected [{start}, {end})"
            )


def _two_anchor_setup(engine, monkeypatch, tmp_path, base, local,
                      safe_target=None, *, html=None):
    doc = make_doc(base)
    merged = make_doc(safe_target, rev="R2") if safe_target is not None else None
    docs = DocsStub(doc, merged_doc=merged)
    comments = [
        api_comment("c1", "A", CREATED),
        api_comment("c2", "B", "2026-07-13T18:10:00.000Z"),
    ]
    entries = [
        ("0", "A", CREATED_SEC),
        ("1", "B", "2026-07-13T18:10:00Z"),
    ]
    paras = []
    for text in base:
        spans = []
        if text == P1:
            spans.append(("0", 0, len(text)))
        if text == P2:
            spans.append(("1", 0, len(text)))
        paras.append((text, spans))
    drive = DriveStub(
        comments,
        _docx_builder(docs, paras, entries),
        html=html if html is not None else _html(safe_target or base),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))
    return doc, docs, drive, path


# ---------------------------------------------------------------------------
# Occurrence-qualified identity
# ---------------------------------------------------------------------------


def test_occurrence_ids_make_the_decisive_twin_move_unambiguous(engine):
    keys = [("p", X), ("p", A), ("p", X)]
    local_keys = [("p", X), ("p", X), ("p", A)]

    base_ids = engine._occurrence_ids(keys)
    local_ids = engine._occurrence_ids(local_keys)

    assert base_ids == [(("p", X), 0), (("p", A), 0), (("p", X), 1)]
    assert local_ids == [(("p", X), 0), (("p", X), 1), (("p", A), 0)]

    status, inserts, mapping = engine._diff_status_pinned(keys, local_keys)
    assert mapping[0] == 0 and mapping[2] == 1
    assert status[0] == status[2] == "equal"
    assert status[1] == "deleted"
    assert inserts == {3: [2]}, "A must move; neither indistinguishable X may move"


def test_sync_moves_the_unique_neighbour_not_either_twin(
        engine, monkeypatch, tmp_path, capsys):
    base = [X, A, X]
    local = [X, X, A]
    doc = make_doc(base)
    docs = run_sync(engine, monkeypatch, tmp_path, base, local, doc=doc)
    out = json.loads(capsys.readouterr().out)

    assert out["moved"] == 1
    requests = _main_text_requests(docs, doc)
    assert replay(base, requests) == local
    _assert_no_delete_of(doc, base, [0, 2], requests)


def test_moved_duplicate_keeps_its_occurrence_style(
        engine, monkeypatch, tmp_path, capsys):
    """Move X#1, not a fresh text-key X and not X#0's appearance."""
    red = {"foregroundColor": {"color": {"rgbColor": {"red": 0.8}}}}
    blue = {"foregroundColor": {"color": {"rgbColor": {"blue": 0.9}}}}
    base = [X, "Y", X, "Y"]
    local = [X, X, "Y", "Y"]
    doc = make_doc_runs([[(X, red)], "Y", [(X, blue)], "Y"])

    docs = run_sync(engine, monkeypatch, tmp_path, base, local, doc=doc)
    out = json.loads(capsys.readouterr().out)

    assert out["moved"] == 1
    assert out["inserted"] == 0 and out["deleted"] == 0
    colors = [
        req["updateTextStyle"]["textStyle"].get("foregroundColor")
        for req in style_batch(docs) if "updateTextStyle" in req
    ]
    assert any("blue" in json.dumps(color) for color in colors if color)


def test_changed_duplicate_multiplicity_defers_only_its_hull(
        engine, monkeypatch, tmp_path, capsys):
    tail, tail_local = "Независимый хвост", "Независимый хвост — готов"
    base = [X, A, X, tail]
    local = [X, A, X, X, tail_local]
    safe = [X, A, X, tail_local]
    doc = make_doc(base)
    docs = DocsStub(doc, merged_doc=make_doc(safe, rev="R2"))
    drive = DriveStub(
        [], _docx_builder(docs, [(text, []) for text in base], []),
        html=_html(safe))
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partially-synced"
    assert out["deferred"]["reason"] == "duplicate-ambiguity"
    assert replay(base, _main_text_requests(docs, doc)) == safe
    rebased = open(path, encoding="utf-8").read()
    assert rebased.count(X) == 3
    assert tail_local in rebased


# ---------------------------------------------------------------------------
# Forced pins and alternatives around them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("local", "forced_local", "moved_base"),
    [
        ([P, A, B], 0, 0),  # move the neighbour above PIN to below it
        ([A, B, P], 2, 2),  # move the neighbour below PIN to above it
    ],
)
def test_pinned_alignment_moves_the_neighbour_in_both_directions(
        engine, local, forced_local, moved_base):
    base = [A, P, B]
    base_keys = [("p", t) for t in base]
    local_keys = [("p", t) for t in local]
    status, inserts, mapping = engine._diff_status_pinned(
        base_keys, local_keys, forced_pairs=((1, forced_local),))

    assert status[1] == "equal" and mapping[1] == forced_local
    assert status[moved_base] == "deleted"
    assert moved_base not in mapping
    assert inserts, "the unprotected neighbour must be inserted on the other side"


@pytest.mark.parametrize("local", [[P, A, B], [A, B, P]])
def test_commented_pin_is_never_deleted_for_either_neighbour_move(
        engine, monkeypatch, tmp_path, capsys, local):
    base = [A, P, B]
    doc = make_doc(base)
    docs = run_sync(
        engine,
        monkeypatch,
        tmp_path,
        base,
        local,
        doc=doc,
        comments=[api_comment("c1", "A", CREATED)],
        anchors={P},
    )
    out = json.loads(capsys.readouterr().out)

    assert out["action"] == "synced"
    requests = _main_text_requests(docs, doc)
    assert replay(base, requests) == local
    _assert_no_delete_of(doc, base, [1], requests)


# ---------------------------------------------------------------------------
# Component-level partial application
# ---------------------------------------------------------------------------


def test_crossing_pins_defer_only_the_crossing_component(
        engine, monkeypatch, tmp_path, capsys):
    tail, tail_edited = "Независимый хвост", "Независимый хвост — исправлен"
    base = [P1, "Середина", P2, tail]
    local = [P2, P1, "Середина", tail_edited]
    safe = [P1, "Середина", P2, tail_edited]
    doc, docs, _drive, path = _two_anchor_setup(
        engine, monkeypatch, tmp_path, base, local, safe)

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partially-synced"
    assert "crossing" in " ".join(_all_strings(out)).lower()
    assert out["applied"] == {
        "replaced": 1,
        "moved": 0,
        "inserted": 0,
        "deleted": 0,
        "style_only": 0,
        "styled_blocks": 1,
    }
    assert out["journal"] == str(path) + ".gdocs-sync-journal.json"
    journal = json.loads(Path(out["journal"]).read_text())
    assert journal["deferred"] == out["deferred"]
    assert journal["phases"][-1] == {
        "phase": "complete",
        "status": "partially-synced",
        "advanced": True,
    }
    requests = _main_text_requests(docs, doc)
    assert replay(base, requests) == safe
    _assert_no_delete_of(doc, base, [0, 2], requests)


def test_partial_noop_cleans_the_canary_and_writes_nothing_else(
        engine, monkeypatch, tmp_path, capsys):
    base, local = [P1, P2], [P2, P1]
    _doc, docs, _drive, path = _two_anchor_setup(
        engine, monkeypatch, tmp_path, base, local)

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partial-noop"
    assert docs.canary_text is None
    assert _no_content_mutation(docs)
    assert len(docs.batches) == 2, "only canary insert + proven cleanup are allowed"


def test_partial_noop_does_not_claim_an_unchanged_doc_if_cleanup_failed(
        engine, monkeypatch, tmp_path, capsys):
    base, local = [P1, P2], [P2, P1]
    _doc, docs, _drive, path = _two_anchor_setup(
        engine, monkeypatch, tmp_path, base, local)
    docs.fail_cleanup = True

    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", path)
    out = json.loads(capsys.readouterr().out)

    assert out.get("action") != "partial-noop"
    assert "skrepka-canary" in " ".join(_all_strings(out))
    assert docs.canary_text is not None


@pytest.mark.parametrize(
    ("base", "local"),
    [
        # The second swap starts at the exact boundary where the crossing
        # component ends; it must not slip through as an independent move.
        ([P1, P2, A, B], [P2, P1, B, A]),
        # An insertion uses the crossing component's target boundary gap.
        ([P1, P2, A], [P2, P1, "Новая граница", A]),
    ],
)
def test_touching_or_shared_gap_operations_join_the_deferred_component(
        engine, monkeypatch, tmp_path, capsys, base, local):
    _doc, docs, _drive, path = _two_anchor_setup(
        engine, monkeypatch, tmp_path, base, local)

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partial-noop"
    assert _no_content_mutation(docs)


def test_replacement_inside_crossing_hull_defers_both_halves(
        engine, monkeypatch, tmp_path, capsys):
    """Restoring only the old half must not leak A-edit as an insertion."""
    tail, tail_local = "Хвост", "Хвост — применён"
    base = [P1, A, P2, tail]
    local = [P2, P1, f"{A} edit", tail_local]
    safe = [P1, A, P2, tail_local]
    doc, docs, _drive, path = _two_anchor_setup(
        engine, monkeypatch, tmp_path, base, local, safe)

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partially-synced"
    requests = _main_text_requests(docs, doc)
    assert replay(base, requests) == safe
    assert replay(base, requests).count(A) == 1
    _assert_no_delete_of(doc, base, [0, 1, 2], requests)


def test_crossing_replacement_pairing_ignores_sequence_matcher_guess(
        engine, monkeypatch, tmp_path, capsys):
    """A later edit must not steal the replacement pair from inside hull."""
    sep, tail, tail_local = "Разделитель", "Хвост", "Хвост — применён"
    base = [P1, A, P2, sep, tail]
    local = [P2, P1, sep, f"{A} edit", tail_local]
    safe = [P1, A, P2, sep, tail_local]
    doc, docs, _drive, path = _two_anchor_setup(
        engine, monkeypatch, tmp_path, base, local, safe)

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partially-synced"
    requests = _main_text_requests(docs, doc)
    assert replay(base, requests) == safe
    assert replay(base, requests).count(A) == 1
    _assert_no_delete_of(doc, base, [0, 1, 2, 3], requests)


def test_existing_origin_cannot_move_inside_composite_anchor(
        engine, monkeypatch, tmp_path, capsys):
    mover, tail, tail_local = "Переносимый блок", "Хвост", "Хвост готов"
    base = [P1, P2, mover, tail]
    local = [P1, mover, P2, tail_local]
    safe = [P1, P2, mover, tail_local]
    doc = make_doc(base)
    docs = DocsStub(doc, merged_doc=make_doc(safe, rev="R2"))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _crossing_docx(
            docs, base, "0", (0, 0), (1, len(P2)),
            [("0", "A", CREATED_SEC)]),
        html=_html(safe),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partially-synced"
    assert out["deferred"]["reason"] == "composite-anchor"
    requests = _main_text_requests(docs, doc)
    assert replay(base, requests) == safe
    _assert_no_delete_of(doc, base, [0, 1, 2], requests)


@pytest.mark.parametrize("cleanup_failed", [False, True])
@pytest.mark.parametrize("supported_note", [False, True])
def test_deferred_materialization_parse_note_is_canary_honest(
        engine, monkeypatch, tmp_path, capsys, cleanup_failed,
        supported_note):
    """A one-element parse note in a deferred hull is not writable.

    The mocked parser shape is the real image/raw shape from ``_md_elements``:
    one ``opaque-md`` element plus a note.  This must refuse after the
    composite canary, with no semantic request; cleanup failure must expose
    the literal service line instead of claiming an unchanged document.
    """
    mover, tail, tail_local = "Переносимый блок", "Хвост", "Хвост готов"
    base = [P1, P2, mover, tail]
    local = [P1, mover, P2, tail_local]
    safe = [P1, P2, mover, tail_local]
    doc = make_doc(base)
    docs = DocsStub(doc, merged_doc=make_doc(safe, rev="R2"))
    comments = [api_comment("c1", "A", CREATED)]
    entries = [("0", "A", CREATED_SEC)]
    drive = DriveStub(
        comments,
        _crossing_docx(
            docs, base, "0", (0, 0), (1, len(P2)), entries),
        html=_html(safe),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))
    docs.fail_cleanup = cleanup_failed

    real_elements = engine._md_elements
    image_elements, image_notes = real_elements("![img](x.png)")
    assert len(image_elements) == 1 and image_notes
    assert image_elements[0]["type"] == "opaque-md"
    calls = 0

    def image_note(md):
        nonlocal calls
        calls += 1
        parsed, problems = real_elements(md)
        if calls > 1 and md == P1:
            if supported_note:
                return [{"type": "p", "text": P1, "sig": "[]", "raw": P1}], [
                    "future unsafe parser note",
                ]
            return [{"type": "opaque-md", "raw": md}], [
                "unsupported inline markdown: image",
            ]
        return parsed, problems

    monkeypatch.setattr(engine, "_md_elements", image_note)
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", path)
    out = json.loads(capsys.readouterr().out)
    assert "cannot materialize" in out["error"]
    assert _no_content_mutation(docs)
    if cleanup_failed:
        assert docs.canary_text is not None
        assert "skrepka-canary" in out["error"]
    else:
        assert docs.canary_text is None


@pytest.mark.parametrize("raw_block", [
    "```python\nprint(1)\n```",
    "<div>raw html</div>",
    "> quoted raw block",
])
def test_real_opaque_markdown_blocks_refuse_after_canary(
        engine, monkeypatch, tmp_path, capsys, raw_block):
    """Code, HTML and blockquote parser atoms are never materialized."""
    real_elements = engine._md_elements
    parsed, problems = real_elements(raw_block)
    assert len(parsed) == 1
    assert parsed[0]["type"] == "opaque-md"
    assert parsed[0]["raw"] == raw_block

    base = [P1, P2, "Переносимый блок", "Хвост"]
    local = [P1, "Переносимый блок", P2, "Хвост готов"]
    safe = [P1, P2, "Переносимый блок", "Хвост готов"]
    doc = make_doc(base)
    docs = DocsStub(doc, merged_doc=make_doc(safe, rev="R2"))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _crossing_docx(
            docs, base, "0", (0, 0), (1, len(P2)),
            [("0", "A", CREATED_SEC)]),
        html=_html(safe),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))
    armed = False
    opaque_same_source = [{**parsed[0], "raw": P1}]

    def opaque_parser(md):
        nonlocal armed
        if armed and md == P1:
            # Deliberately make source equality pass: the kind allowlist is
            # the only remaining safety boundary for this probe.
            return opaque_same_source, []
        armed = True
        return real_elements(md)

    monkeypatch.setattr(engine, "_md_elements", opaque_parser)
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", path)
    out = json.loads(capsys.readouterr().out)
    assert "cannot materialize" in out["error"]
    assert docs.canary_text is None
    assert _no_content_mutation(docs)


@pytest.mark.parametrize(
    ("hull", "base", "local", "safe", "target"),
    [
        (
            "crossing-pins",
            [P1, A, P2, "Хвост"],
            [P2, P1, f"{A} edit", "Хвост готов"],
            [P1, A, P2, "Хвост готов"],
            A,
        ),
        (
            "duplicate-ambiguity",
            [X, X, P1],
            [X, P1],
            [X, P1],
            X,
        ),
    ],
)
def test_deferred_hull_classes_refuse_image_parse_note_without_write(
        engine, monkeypatch, tmp_path, capsys, hull, base, local, safe, target):
    """Crossing and duplicate hulls share the same image/raw refusal."""
    doc = make_doc(base)
    docs = DocsStub(doc, merged_doc=make_doc(safe, rev="R2"))
    if hull == "crossing-pins":
        comments = [
            api_comment("c1", "A", CREATED),
            api_comment("c2", "B", "2026-07-13T18:10:00.000Z"),
        ]
        entries = [
            ("0", "A", CREATED_SEC),
            ("1", "B", "2026-07-13T18:10:00Z"),
        ]
        paras = [
            (text, [("0", 0, len(text))] if text == P1 else
             [("1", 0, len(text))] if text == P2 else [])
            for text in base
        ]
    else:
        comments = [api_comment("c1", "A", CREATED)]
        entries = [("0", "A", CREATED_SEC)]
        paras = [(text, [("0", 0, len(text))] if text == P1 else [])
                 for text in base]
    drive = DriveStub(comments, _docx_builder(docs, paras, entries),
                      html=_html(safe))
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))
    real_elements = engine._md_elements
    armed = False

    def image_note(md):
        nonlocal armed
        parsed, problems = real_elements(md)
        if armed and md == target:
            return [{"type": "opaque-md", "raw": md}], [
                "unsupported inline markdown: image",
            ]
        armed = True
        return parsed, problems

    monkeypatch.setattr(engine, "_md_elements", image_note)
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", path)
    out = json.loads(capsys.readouterr().out)
    assert "cannot materialize" in out["error"], hull
    assert _no_content_mutation(docs)
    assert docs.canary_text is None


def test_cost_limit_materialization_parse_note_is_fail_closed(
        engine, monkeypatch, tmp_path, capsys):
    """The cost-deferred origin path cannot smuggle an image through."""
    _set_caps(engine, monkeypatch, blocks=4, units=40)
    long1, long2 = "LLLLLLLLL1", "LLLLLLLLL2"
    fillers = [f"stable-{i}" for i in range(13)]
    base = ["a", P1, long1, "s1", "s2", long2,
            "t1", "t2", *fillers]
    local = [P1, "a", "s1", "s2", long1, "t1", "t2", long2,
             *fillers]
    doc = make_doc(base)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(
            docs,
            [(text, [("0", 0, len(P1))] if text == P1 else [])
             for text in base],
            [("0", "A", CREATED_SEC)]),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))
    real_elements = engine._md_elements
    armed = False

    def image_note(md):
        nonlocal armed
        parsed, problems = real_elements(md)
        if armed and md == "a":
            return [{"type": "opaque-md", "raw": md}], [
                "unsupported inline markdown: image",
            ]
        armed = True
        return parsed, problems

    monkeypatch.setattr(engine, "_md_elements", image_note)
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", path)
    out = json.loads(capsys.readouterr().out)
    assert "cannot materialize" in out["error"]
    assert _no_content_mutation(docs)
    assert docs.canary_text is None


def test_composite_and_independent_crossing_are_localized_together(
        engine, monkeypatch, tmp_path, capsys):
    import io
    import zipfile

    c1, c2, mover, sep, tail, tail_local = (
        "Составной один", "Составной два", "Переносимый", "Разделитель",
        "Хвост", "Хвост готов")
    base = [c1, c2, mover, sep, P1, P2, tail]
    local = [f"**{c1}**", mover, c2, sep, P2, P1, tail_local]
    safe = [c1, c2, mover, sep, P1, P2, tail_local]
    doc = make_doc(base)
    docs = DocsStub(doc, merged_doc=make_doc(safe, rev="R2"))
    comments = [
        api_comment("c0", "C", CREATED),
        api_comment("c1", "A", "2026-07-13T18:10:00.000Z"),
        api_comment("c2", "B", "2026-07-13T18:11:00.000Z"),
    ]

    def export_docx():
        texts = list(base)
        if docs.canary_text is not None:
            texts.append(docs.canary_text)
        paras = []
        for i, text_value in enumerate(texts):
            before = after = ""
            if i == 0:
                before = '<w:commentRangeStart w:id="0"/>'
            elif i == 1:
                after = ('<w:commentRangeEnd w:id="0"/>'
                         '<w:r><w:commentReference w:id="0"/></w:r>')
            elif i == 4:
                before = '<w:commentRangeStart w:id="1"/>'
                after = ('<w:commentRangeEnd w:id="1"/>'
                         '<w:r><w:commentReference w:id="1"/></w:r>')
            elif i == 5:
                before = '<w:commentRangeStart w:id="2"/>'
                after = ('<w:commentRangeEnd w:id="2"/>'
                         '<w:r><w:commentReference w:id="2"/></w:r>')
            paras.append(
                f'<w:p>{before}<w:r><w:t>{text_value}</w:t></w:r>'
                f'{after}</w:p>')
        wordml = (
            "http://schemas.openxmlformats.org/wordprocessingml/2006/main")
        document = (f'<w:document xmlns:w="{wordml}"><w:body>'
                    f'{"".join(paras)}</w:body></w:document>')
        entries = [
            ("0", "C", CREATED_SEC),
            ("1", "A", "2026-07-13T18:10:00Z"),
            ("2", "B", "2026-07-13T18:11:00Z"),
        ]
        records = "".join(
            f'<w:comment w:id="{cid}" w:author="{author}" '
            f'w:date="{date}"><w:p/></w:comment>'
            for cid, author, date in entries)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr(
                "word/comments.xml",
                f'<w:comments xmlns:w="{wordml}">{records}</w:comments>')
        return buffer.getvalue()

    drive = DriveStub(comments, export_docx, html=_html(safe))
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partially-synced"
    assert "composite-anchor" in out["deferred"]["reason"]
    assert "crossing-pins" in out["deferred"]["reason"]
    assert docs.canary_text is None
    requests = _main_text_requests(docs, doc)
    assert replay(base, requests) == safe


def test_deleting_every_duplicate_cleans_canary_instead_of_raising(
        engine, monkeypatch, tmp_path, capsys):
    base, local = [X, X, P], [P]
    doc = make_doc(base)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(
            docs,
            [(X, []), (X, []), (P, [("0", 0, len(P))])],
            [("0", "A", CREATED_SEC)],
        ),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partial-noop"
    assert docs.canary_text is None
    assert _no_content_mutation(docs)
    assert len(docs.batches) == 2


# ---------------------------------------------------------------------------
# Partial lifecycle: fresh remote base + deferred intent
# ---------------------------------------------------------------------------


def test_partial_rebase_survives_a_second_run_without_reverting_remote_work(
        engine, monkeypatch, tmp_path, capsys):
    """Remote text+bold outside the hull survives deferred crossing twice."""
    old_x, remote_x = "Удалённый исходник", "Правка коллеги"
    tail, tail_local = "Хвост", "Хвост пользователя"
    base = [P1, P2, old_x, tail]
    local = [P2, P1, old_x, tail_local]
    safe_texts = [P1, P2, remote_x, tail_local]

    sidecar_doc = make_doc(base)
    live = make_doc_runs([P1, P2, [(remote_x, {"bold": True})], tail])
    safe_doc = make_doc_runs([P1, P2, [(remote_x, {"bold": True})], tail_local])
    safe_doc["revisionId"] = "R2"
    docs = DocsStub(live, merged_doc=safe_doc)
    comments = [
        api_comment("c1", "A", CREATED),
        api_comment("c2", "B", "2026-07-13T18:10:00.000Z"),
    ]
    entries = [
        ("0", "A", CREATED_SEC),
        ("1", "B", "2026-07-13T18:10:00Z"),
    ]
    drive = DriveStub(
        comments,
        _docx_builder(
            docs,
            [(P1, [("0", 0, len(P1))]),
             (P2, [("1", 0, len(P2))]),
             (remote_x, []),
             (tail, [])],
            entries,
        ),
        html=(f"<p>{P1}</p><p>{P2}</p><p><strong>{remote_x}</strong></p>"
              f"<p>{tail_local}</p>").encode(),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, sidecar_doc, _md(base), _md(local))

    first = _partial_call(engine, capsys, "doc1", path)
    assert first["action"] == "partially-synced"
    rebased = open(path, encoding="utf-8").read()
    assert rebased.index(P2) < rebased.index(P1), "deferred crossing was lost"
    assert f"**{remote_x}**" in rebased, "remote text/style was reverted"
    assert old_x not in rebased
    assert tail_local in rebased

    sidecar = json.load(open(path + engine.SIDECAR_SUFFIX, encoding="utf-8"))
    assert [e.get("text") for e in sidecar["elements"]] == safe_texts
    assert sidecar["md_sha256"] != engine._sha256_str(rebased), (
        "the sidecar hash names the fresh remote merge-base, not working intent"
    )

    # Run again from the freshly advanced remote base.  Only the crossing is
    # left; the collaborator's text/bold must not become a local revert and
    # the already-applied tail edit must not be replayed.
    docs2 = DocsStub(safe_doc)
    drive2 = DriveStub(
        comments,
        _docx_builder(
            docs2,
            [(P1, [("0", 0, len(P1))]),
             (P2, [("1", 0, len(P2))]),
             (remote_x, []),
             (tail_local, [])],
            entries,
        ),
        html=(f"<p>{P1}</p><p>{P2}</p><p><strong>{remote_x}</strong></p>"
              f"<p>{tail_local}</p>").encode(),
    )
    wire(engine, monkeypatch, docs2, drive2)
    before_second = open(path, encoding="utf-8").read()

    second = _partial_call(engine, capsys, "doc1", path)

    assert second["action"] == "partial-noop"
    assert open(path, encoding="utf-8").read() == before_second
    assert _no_content_mutation(docs2)


def test_partial_rebase_keeps_local_style_inside_deferred_hull(
        engine, monkeypatch, tmp_path, capsys):
    tail, tail_local = "Хвост", "Хвост применён"
    base = [P1, P2, tail]
    local = [P2, f"**{P1}**", tail_local]
    safe = [P1, P2, tail_local]
    _doc, _docs, _drive, path = _two_anchor_setup(
        engine, monkeypatch, tmp_path, base, local, safe)

    out = _partial_call(engine, capsys, "doc1", path)
    rebased = open(path, encoding="utf-8").read()

    assert out["action"] == "partially-synced"
    assert f"**{P1}**" in rebased
    assert rebased.index(P2) < rebased.index(P1)


def test_deferred_hull_refuses_simultaneous_local_and_remote_style(
        engine, monkeypatch, tmp_path, capsys):
    tail, tail_local = "Хвост", "Хвост применён"
    base = [P1, P2, tail]
    local = [P2, f"**{P1}**", tail_local]
    sidecar_doc = make_doc(base)
    live = make_doc_runs([[(P1, {"italic": True})], P2, tail])
    docs = DocsStub(live)
    comments = [
        api_comment("c1", "A", CREATED),
        api_comment("c2", "B", "2026-07-13T18:10:00.000Z"),
    ]
    entries = [
        ("0", "A", CREATED_SEC),
        ("1", "B", "2026-07-13T18:10:00Z"),
    ]
    drive = DriveStub(
        comments,
        _docx_builder(
            docs,
            [(P1, [("0", 0, len(P1))]),
             (P2, [("1", 0, len(P2))]),
             (tail, [])],
            entries),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(
        engine, tmp_path, sidecar_doc, _md(base), _md(local))

    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", path)
    out = json.loads(capsys.readouterr().out)

    assert out.get("action") not in {"partially-synced", "partial-noop"}
    assert _no_content_mutation(docs)
    assert docs.canary_text is None


def test_partial_origin_graph_preserves_remote_insert_and_advances_base(
        engine, monkeypatch, tmp_path, capsys):
    sep, remote_insert = "Разделитель", "Вставка коллеги"
    tail, tail_local = "Хвост", "Хвост применён"
    base = [P1, P2, sep, tail]
    local = [P2, P1, sep, tail_local]
    live_texts = [P1, P2, sep, remote_insert, tail]
    safe_texts = [P1, P2, sep, remote_insert, tail_local]
    sidecar_doc = make_doc(base)
    live = make_doc(live_texts)
    docs = DocsStub(live, merged_doc=make_doc(safe_texts, rev="R2"))
    comments = [
        api_comment("c1", "A", CREATED),
        api_comment("c2", "B", "2026-07-13T18:10:00.000Z"),
    ]
    entries = [
        ("0", "A", CREATED_SEC),
        ("1", "B", "2026-07-13T18:10:00Z"),
    ]
    drive = DriveStub(
        comments,
        _docx_builder(
            docs,
            [(P1, [("0", 0, len(P1))]),
             (P2, [("1", 0, len(P2))]),
             (sep, []), (remote_insert, []), (tail, [])],
            entries),
        html=_html(safe_texts),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(
        engine, tmp_path, sidecar_doc, _md(base), _md(local))
    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partially-synced"
    assert out["advanced"] is True
    working, problems = engine._md_elements(open(
        path, encoding="utf-8").read())
    assert not problems
    assert [el.get("text") for el in working] == [
        P2, P1, sep, remote_insert, tail_local]
    sidecar = json.load(open(
        path + engine.SIDECAR_SUFFIX, encoding="utf-8"))
    assert [el.get("text") for el in sidecar["elements"]] == safe_texts


# ---------------------------------------------------------------------------
# Multi-objective cost frontier and aggregate budgets
# ---------------------------------------------------------------------------


def _set_caps(engine, monkeypatch, *, blocks, units, text=100, style=100):
    monkeypatch.setattr(engine, "_MAX_PINNED_MOVE_BLOCKS", blocks, raising=False)
    monkeypatch.setattr(
        engine, "_MAX_PINNED_DESTRUCTIVE_UNITS", units, raising=False)
    monkeypatch.setattr(engine, "_MAX_PINNED_TEXT_REQUESTS", text, raising=False)
    monkeypatch.setattr(engine, "_MAX_PINNED_STYLE_REQUESTS", style, raising=False)


def test_pareto_frontier_keeps_a_feasible_intermediate_alignment(
        engine, monkeypatch, tmp_path, capsys):
    """Both extreme optima fail, while a mixed alignment fits every cap."""
    _set_caps(engine, monkeypatch, blocks=4, units=40)
    long1, long2 = "LLLLLLLLL1", "LLLLLLLLL2"  # move cost 22 each
    s1, s2, t1, t2 = "s1", "s2", "t1", "t2"  # move cost 6 each
    fixed = "a"                                  # pinned detour cost 4
    base = [fixed, P, long1, s1, s2, long2, t1, t2]
    local = [P, fixed, s1, s2, long1, t1, t2, long2]
    doc = make_doc(base)
    docs = run_sync(
        engine,
        monkeypatch,
        tmp_path,
        base,
        local,
        doc=doc,
        comments=[api_comment("c1", "A", CREATED)],
        anchors={P},
    )
    out = json.loads(capsys.readouterr().out)

    # count-optimal: fixed + two longs = 3 blocks / 48 units (fails units)
    # unit-optimal: fixed + four shorts = 5 blocks / 28 units (fails blocks)
    # mixed: fixed + one long + two shorts = 4 blocks / 38 units (fits both)
    assert out["action"] == "synced"
    assert out["moved"] == 4
    assert replay(base, _main_text_requests(docs, doc)) == local


def test_weighted_alignment_runs_even_when_first_plan_fits_caps(
        engine, monkeypatch, tmp_path, capsys):
    """Caps do not make a needlessly destructive equal-size plan optimal."""
    short, long = "s", "L" * 40
    base = ["a", P, short, long]
    local = [P, "a", long, short]
    doc = make_doc(base)

    docs = run_sync(
        engine, monkeypatch, tmp_path, base, local, doc=doc,
        comments=[api_comment("c1", "A", CREATED)], anchors={P})
    out = json.loads(capsys.readouterr().out)

    assert out["action"] == "synced"
    assert out["moved"] == 2
    assert out["planner"]["strategy"] == "exact-pareto"
    assert out["planner"]["optimality_proven"] is True
    journal = json.load(open(out["journal"], encoding="utf-8"))
    assert journal["planner"] == out["planner"]
    requests = _main_text_requests(docs, doc)
    assert replay(base, requests) == local
    _assert_no_delete_of(doc, base, [1, 3], requests)


def test_over_twenty_cost_hole_is_componentized_with_honest_metadata(
        engine, monkeypatch, tmp_path, capsys):
    _set_caps(engine, monkeypatch, blocks=4, units=40)
    long1, long2 = "LLLLLLLLL1", "LLLLLLLLL2"
    fixed = "a"
    fillers = [f"unchanged-{i}" for i in range(13)]
    base = [fixed, P, long1, "s1", "s2", long2, "t1", "t2", *fillers]
    local = [P, fixed, "s1", "s2", long1, "t1", "t2", long2, *fillers]
    doc = make_doc(base)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(
            docs,
            [(text, [("0", 0, len(P))] if text == P else [])
             for text in base],
            [("0", "A", CREATED_SEC)],
        ),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partial-noop"
    assert out["deferred"]["reason"] == "cost-limit"
    assert out["deferred"]["planner"] == {
        "strategy": "constrained-lis",
        "optimality_proven": False,
        "origin_count": len(base),
    }
    assert out["planner"] == out["deferred"]["planner"]
    journal = json.load(open(out["journal"], encoding="utf-8"))
    assert journal["planner"] == out["planner"]
    assert journal["deferred"]["deferred_origin_intent"]
    assert _no_content_mutation(docs)
    assert docs.canary_text is None


def test_final_request_gate_remains_insurance_for_large_planner_mismatch(
        engine, monkeypatch, tmp_path, capsys):
    """The measured request cap survives a future accounting regression."""
    _set_caps(engine, monkeypatch, blocks=4, units=40)
    long1, long2 = "LLLLLLLLL1", "LLLLLLLLL2"
    fillers = [f"unchanged-{i}" for i in range(13)]
    base = ["a", P, long1, "s1", "s2", long2, "t1", "t2", *fillers]
    local = [P, "a", "s1", "s2", long1, "t1", "t2", long2, *fillers]
    doc = make_doc(base)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(
            docs,
            [(text, [("0", 0, len(P))] if text == P else [])
             for text in base],
            [("0", "A", CREATED_SEC)],
        ),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))
    normalize = engine._normalized_permutation_alignment

    def undercounted(*args, **kwargs):
        result = normalize(*args, **kwargs)
        if result is not None and result["origin_count"] > 20:
            result = {**result, "moved_origins": []}
        return result

    monkeypatch.setattr(
        engine, "_normalized_permutation_alignment", undercounted)

    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", path)

    error = json.loads(capsys.readouterr().out)["error"]
    assert "hard safety caps" in error
    assert "optimality_proven=false" in error
    assert _no_content_mutation(docs)
    assert docs.canary_text is None


def test_over_twenty_independent_cost_corridors_apply_cheapest_component(
        engine, monkeypatch, tmp_path, capsys):
    _set_caps(engine, monkeypatch, blocks=1, units=1_000)
    fillers = [f"stable-{i}" for i in range(18)]
    base = ["a", P1, *fillers, "BBBB", P2]
    local = [P1, "a", *fillers, P2, "**BBBB**"]
    intent_texts = [P1, "a", *fillers, P2, "BBBB"]
    safe = [P1, "a", *fillers, "BBBB", P2]
    calls = []
    normalize = engine._normalized_permutation_alignment

    def observed(*args, **kwargs):
        result = normalize(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(engine, "_normalized_permutation_alignment", observed)
    doc, docs, _drive, path = _two_anchor_setup(
        engine, monkeypatch, tmp_path, base, local, safe)

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partially-synced"
    assert out["moved"] == 1
    assert out["deferred"]["planner"]["optimality_proven"] is False
    assert out["deferred"]["planner"]["strategy"] == "constrained-lis"
    assert out["planner"] == out["deferred"]["planner"]
    assert "safe_origin_order" not in out["deferred"]
    assert len(calls) >= 2, "selected origin order must be normalized again"
    assert replay(base, _main_text_requests(docs, doc)) == safe
    assert [el["text"] for el in engine._md_elements(
        open(path, encoding="utf-8").read())[0]] == intent_texts
    assert "**BBBB**" in open(path, encoding="utf-8").read(), (
        "deferred moved origin must retain its exact local style intent")
    sidecar = json.load(open(
        path + engine.SIDECAR_SUFFIX, encoding="utf-8"))
    assert [el["text"] for el in sidecar["elements"]] == safe
    assert next(el for el in sidecar["elements"]
                if el["text"] == "BBBB")["raw_md"] == "BBBB"
    journal = json.load(open(out["journal"], encoding="utf-8"))
    assert journal["deferred"]["planner"]["optimality_proven"] is False
    assert journal["planner"] == out["planner"]
    assert journal["deferred"]["deferred_origin_intent"] == [{
        "base_index": 20,
        "intent_local_index": 21,
    }]

    # The next run starts from the fresh plain safe sidecar while the working
    # file still carries both the deferred precedence and bold intent.  With
    # one remaining move it must complete, not silently flatten the style.
    final = intent_texts
    docs2 = DocsStub(make_doc(safe, rev="R2"),
                     merged_doc=make_doc(final, rev="R4"))
    comments = [
        api_comment("c1", "A", CREATED),
        api_comment("c2", "B", "2026-07-13T18:10:00.000Z"),
    ]
    entries = [
        ("0", "A", CREATED_SEC),
        ("1", "B", "2026-07-13T18:10:00Z"),
    ]
    paras = [
        (text,
         [("0", 0, len(text))] if text == P1 else
         [("1", 0, len(text))] if text == P2 else [])
        for text in safe
    ]
    final_html = "".join(
        f"<p><strong>{text}</strong></p>" if text == "BBBB"
        else f"<p>{text}</p>"
        for text in final
    ).encode()
    drive2 = DriveStub(
        comments, _docx_builder(docs2, paras, entries), html=final_html)
    wire(engine, monkeypatch, docs2, drive2)

    engine.sync_doc("doc1", path)
    second = json.loads(capsys.readouterr().out)

    assert second["action"] == "synced"
    assert second["moved"] == 1
    assert any(
        (request.get("updateTextStyle") or {}).get(
            "textStyle", {}).get("bold") is True
        for batch in docs2.batches for request in batch
    ), "the second run must send the deferred bold style to Docs"
    final_working = open(path, encoding="utf-8").read()
    assert final_working.rstrip().endswith("**BBBB**")


def test_large_alignment_has_deterministic_tie_break_and_opaque_barrier(
        engine):
    base = [("p", f"block-{i}") for i in range(21)]
    base[10] = ("opaque", "opaque-hash")
    local = list(base)
    local[0], local[1] = local[1], local[0]
    local[9], local[11] = local[11], local[9]
    costs = [
        {"blocks": 1, "units": 1, "text": 2, "style": 0}
        for _key in base
    ]
    caps = {"blocks": 500, "units": 500, "text": 500, "style": 500}

    first = engine._normalized_permutation_alignment(
        base, local, costs, set(), caps)
    second = engine._normalized_permutation_alignment(
        base, local, costs, set(), caps)

    assert first == second
    assert first["strategy"] == "constrained-lis"
    assert first["optimality_proven"] is False
    assert first["moved_origins"] == [0, 9, 11]
    assert first["mapping"][10] == 10


def test_over_twenty_under_cap_full_success_reports_unproven_planner(
        engine, monkeypatch, tmp_path, capsys):
    fillers = [f"stable-{i}" for i in range(19)]
    base = ["a", P, *fillers]
    local = [P, "a", *fillers]

    run_sync(
        engine, monkeypatch, tmp_path, base, local,
        comments=[api_comment("c1", "A", CREATED)], anchors={P})
    out = json.loads(capsys.readouterr().out)

    assert out["action"] == "synced"
    assert out["planner"] == {
        "strategy": "constrained-lis",
        "optimality_proven": False,
        "origin_count": len(base),
    }
    journal = json.load(open(out["journal"], encoding="utf-8"))
    assert journal["planner"] == out["planner"]
    assert journal["phases"][-1]["status"] == "synced"


def test_public_deferred_summary_bounds_origin_metadata(engine):
    plan = {
        "reason": "cost-limit",
        "safe_origin_order": list(range(100)),
        "deferred_base_indices": list(range(100)),
        "deferred_origin_intent": [
            {"base_index": i, "intent_local_index": 99 - i}
            for i in range(100)
        ],
    }

    public = engine._public_deferred_summary(plan)

    assert "safe_origin_order" not in public
    assert len(public["deferred_base_indices"]) == 8
    assert public["deferred_base_indices_count"] == 100
    assert len(public["deferred_base_indices_sha256"]) == 64
    assert len(public["deferred_origin_intent"]) == 8
    assert public["deferred_origin_intent_count"] == 100
    assert len(json.dumps(public, ensure_ascii=False)) < 2_000


def test_caps_are_aggregate_across_independent_pinned_detours(
        engine, monkeypatch, tmp_path, capsys):
    _set_caps(engine, monkeypatch, blocks=1, units=1_000)
    sep, longer = "Неподвижный разделитель", "BBBB"
    base = ["a", P1, sep, longer, P2]
    local = [P1, "a", sep, P2, longer]
    # Each detour is one block and passes per-component.  The aggregate cap
    # admits only the cheaper first component.
    safe = [P1, "a", sep, longer, P2]
    doc, docs, _drive, path = _two_anchor_setup(
        engine, monkeypatch, tmp_path, base, local, safe)

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partially-synced"
    assert out["moved"] == 1
    assert "cost-limit" in " ".join(_all_strings(out))
    requests = _main_text_requests(docs, doc)
    assert replay(base, requests) == safe
    _assert_no_delete_of(doc, base, [1, 4], requests)
    rebased = open(path, encoding="utf-8").read()
    assert rebased.index(P1) < rebased.index("a")
    assert rebased.index(P2) < rebased.index(longer), (
        "the deferred second detour must remain in the rebased working file"
    )


def test_style_cap_uses_actual_fragmented_restore_requests(
        engine, monkeypatch, tmp_path, capsys):
    _set_caps(engine, monkeypatch, blocks=10, units=1_000, text=100, style=5)
    fragmented = "abcdef"
    runs = [
        (char, {"foregroundColor": {"color": {"rgbColor": {
            "red": (i + 1) / 10,
        }}}}) for i, char in enumerate(fragmented)
    ]
    base, local = [fragmented, P], [P, fragmented]
    doc = make_doc_runs([runs, P])

    with pytest.raises(SystemExit) as exc:
        run_sync(
            engine, monkeypatch, tmp_path, base, local, doc=doc,
            comments=[api_comment("c1", "A", CREATED)], anchors={P})
    assert exc.value.code == 3
    out = json.loads(capsys.readouterr().out)

    assert out["action"] == "partial-noop"
    assert out["deferred"]["reason"] == "cost-limit"
    assert out["deferred"]["aggregate_cost"]["style"] > 5


def test_cost_filter_never_applies_half_of_one_move_corridor(
        engine, monkeypatch, tmp_path, capsys):
    _set_caps(engine, monkeypatch, blocks=1, units=1_000)
    long, short = "Длинный сосед", "b"
    base, local = [long, short, P], [P, long, short]

    with pytest.raises(SystemExit) as exc:
        run_sync(
            engine, monkeypatch, tmp_path, base, local,
            comments=[api_comment("c1", "A", CREATED)], anchors={P})
    assert exc.value.code == 3
    out = json.loads(capsys.readouterr().out)

    assert out["action"] == "partial-noop"
    assert out["deferred"]["reason"] == "cost-limit"
