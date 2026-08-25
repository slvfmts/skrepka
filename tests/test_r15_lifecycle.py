"""Adversarial contract for the r15 partial-sync lifecycle.

The executable partial plan is intentionally smaller than the user's full
working intent.  After that plan lands, lifecycle must rebase the deferred
intent by origin identity.  A positional ``len(fresh) == len(base)`` check is
not an origin proof: insert/delete pairs can keep the count unchanged while
shifting every later payload.

These tests specify the missing r15 contract and are expected to stay RED
until the origin graph described in ``internal/PLAN-r15-work.md`` ships.
"""

import json

import pytest

from test_r15 import P1, P2, _html, _md, _partial_call
from test_sync_anchors import (
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


def _partial_lifecycle_setup(
        engine, monkeypatch, tmp_path, *, base, local, remote, safe,
        html=None):
    """Build a crossing-pin partial run with independent outside work."""
    base_doc = make_doc(base)
    live_doc = make_doc(remote)
    docs = DocsStub(live_doc, merged_doc=make_doc(safe, rev="R2"))
    comments = [
        api_comment("c1", "A", CREATED),
        api_comment("c2", "B", "2026-07-13T18:10:00.000Z"),
    ]
    entries = [
        ("0", "A", CREATED_SEC),
        ("1", "B", "2026-07-13T18:10:00Z"),
    ]
    paras = []
    for text in remote:
        spans = []
        if text == P1:
            spans.append(("0", 0, len(P1)))
        if text == P2:
            spans.append(("1", 0, len(P2)))
        paras.append((text, spans))
    drive = DriveStub(
        comments,
        _docx_builder(docs, paras, entries),
        html=_html(safe) if html is None else html,
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(
        engine, tmp_path, base_doc, _md(base), _md(local))
    return docs, path


def _blocks(engine, path):
    parsed, problems = engine._md_elements(
        open(path, encoding="utf-8").read())
    assert not problems
    return [block.get("text") for block in parsed]


def _sidecar_texts(engine, path):
    with open(path + engine.SIDECAR_SUFFIX, encoding="utf-8") as stream:
        payload = json.load(stream)
    return [block.get("text") for block in payload["elements"]]


@pytest.mark.parametrize(
    ("case", "base", "local", "remote", "safe", "rebased"),
    [
        (
            "remote insert",
            [P1, P2, "Разделитель", "Хвост"],
            [P2, P1, "Разделитель", "Хвост локальный"],
            [P1, P2, "Разделитель", "Вставка коллеги", "Хвост"],
            [P1, P2, "Разделитель", "Вставка коллеги", "Хвост локальный"],
            [P2, P1, "Разделитель", "Вставка коллеги", "Хвост локальный"],
        ),
        (
            "remote delete",
            [P1, P2, "Разделитель", "Удалено коллегой", "Хвост"],
            [P2, P1, "Разделитель", "Удалено коллегой", "Хвост локальный"],
            [P1, P2, "Разделитель", "Хвост"],
            [P1, P2, "Разделитель", "Хвост локальный"],
            [P2, P1, "Разделитель", "Хвост локальный"],
        ),
        (
            "local insert",
            [P1, P2, "Разделитель", "Хвост"],
            [P2, P1, "Разделитель", "Вставка пользователя", "Хвост"],
            [P1, P2, "Разделитель", "Хвост"],
            [P1, P2, "Разделитель", "Вставка пользователя", "Хвост"],
            [P2, P1, "Разделитель", "Вставка пользователя", "Хвост"],
        ),
        (
            "local delete",
            [P1, P2, "Разделитель", "Удалено пользователем", "Хвост"],
            [P2, P1, "Разделитель", "Хвост"],
            [P1, P2, "Разделитель", "Удалено пользователем", "Хвост"],
            [P1, P2, "Разделитель", "Хвост"],
            [P2, P1, "Разделитель", "Хвост"],
        ),
    ],
)
def test_partial_rebase_maps_outside_cardinality_changes_by_origin(
        engine, monkeypatch, tmp_path, capsys, case, base, local, remote,
        safe, rebased):
    """Independent insert/delete must not turn into a mapping hole."""
    _docs, path = _partial_lifecycle_setup(
        engine, monkeypatch, tmp_path, base=base, local=local,
        remote=remote, safe=safe)

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["advanced"] is True, case
    assert _blocks(engine, path) == rebased, case
    # The sidecar is the fresh remote merge-base, not the deferred working
    # ordering.  The differing order is deliberate and must survive together.
    assert _sidecar_texts(engine, path) == safe, case


@pytest.mark.parametrize(
    ("case", "base", "local", "remote", "safe", "rebased"),
    [
        (
            "remote insert cancels local delete",
            [P1, P2, "Разделитель", "Локально удалено", "Хвост"],
            [P2, P1, "Разделитель", "Хвост локальный"],
            [P1, P2, "Разделитель", "Вставка коллеги", "Локально удалено", "Хвост"],
            [P1, P2, "Разделитель", "Вставка коллеги", "Хвост локальный"],
            [P2, P1, "Разделитель", "Вставка коллеги", "Хвост локальный"],
        ),
        (
            "remote delete cancels local insert",
            [P1, P2, "Разделитель", "Удалено коллегой", "Хвост"],
            [P2, P1, "Разделитель", "Удалено коллегой", "Вставка пользователя", "Хвост"],
            [P1, P2, "Разделитель", "Хвост"],
            [P1, P2, "Разделитель", "Вставка пользователя", "Хвост"],
            [P2, P1, "Разделитель", "Вставка пользователя", "Хвост"],
        ),
    ],
)
def test_equal_cardinality_is_not_treated_as_an_origin_mapping_proof(
        engine, monkeypatch, tmp_path, capsys, case, base, local, remote,
        safe, rebased):
    """Net-zero count deltas must neither drop nor duplicate a payload."""
    _docs, path = _partial_lifecycle_setup(
        engine, monkeypatch, tmp_path, base=base, local=local,
        remote=remote, safe=safe)

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["advanced"] is True, case
    assert _blocks(engine, path) == rebased, case
    assert _sidecar_texts(engine, path) == safe, case


def test_unprovable_export_mapping_recovery_keeps_fresh_and_deferred_payloads(
        engine, monkeypatch, tmp_path, capsys):
    """A mapping-hole recovery must not be only a fresh-export backup.

    The scripted HTML has an extra block that is absent from the held Docs
    snapshot.  That deliberately breaks the required fresh-doc -> markdown
    bijection, so advancing the merge-base would be dishonest.  The recovery
    artifact still has to carry both the fresh material and exact local
    deferred raw markdown for manual reconciliation.
    """
    tail, tail_local = "Хвост", "Хвост локальный"
    extra = "Непривязанный блок экспорта"
    base = [P1, P2, tail]
    local = [P2, f"**{P1}**", tail_local]
    remote = list(base)
    safe = [P1, P2, tail_local]
    html = _html([P1, P2, extra, tail_local])
    _docs, path = _partial_lifecycle_setup(
        engine, monkeypatch, tmp_path, base=base, local=local,
        remote=remote, safe=safe, html=html)
    sidecar_before = open(
        path + engine.SIDECAR_SUFFIX, encoding="utf-8").read()

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["advanced"] is False
    assert open(path + engine.SIDECAR_SUFFIX, encoding="utf-8").read() == \
        sidecar_before
    recovery = out.get("recovery") or path + ".merged.md"
    recovered = open(recovery, encoding="utf-8").read()
    assert extra in recovered, "fresh export payload was lost"
    assert tail_local in recovered, "already-applied safe work was lost"
    assert f"**{P1}**" in recovered, "deferred local raw style was lost"
    assert P2 in recovered, "deferred local precedence payload was lost"
    assert out.get("recovery") == recovery, (
        "the machine-readable receipt must name the recovery artifact")
