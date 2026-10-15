"""Находки ревью кода r20 (независимый агент, 15.10) — каждая была
воспроизведена падающим тестом на коде до починки.

`--base`: защита снималась после первой операции, на повторе внутри операции
и на своей же канарейке доказательства «одни призраки». Холостой прогон
обещал `would_apply` на документе из призраков с недоказуемой границей
вкладки, а писатель там отказывает.
"""
import io
import json
import zipfile

import pytest  # noqa: F401

from test_issue50_dry_run import (  # noqa: F401
    _W, _ops_file, _thread, _two_tab_doc, _wire_export)
from test_issue52_replace_anchor import (  # noqa: F401
    _body, _patch_with_base, _sidecar, _twins)



def test_second_op_writes_after_first_refused_by_base(engine, monkeypatch, tmp_path, capsys):
    docs = _twins(engine, monkeypatch)
    real = engine._safe_get_doc
    reads = []

    def racing(service, file_id):
        doc = real(service, file_id)
        reads.append(doc.get("revisionId"))
        if len(reads) == 1:
            docs.rev = "R-foreign"
        return doc
    monkeypatch.setattr(engine, "_safe_get_doc", racing)
    ops = [{"op": "replace_quote", "quote": "Копия", "occurrence": 1, "with": "П1"},
           {"op": "replace_quote", "quote": "Копия", "occurrence": 2, "with": "П2"}]
    _code, out = _patch_with_base(engine, tmp_path, capsys, ops,
                                  _sidecar(engine, tmp_path, "R0"))
    # Чужая правка между сверкой и первой записью останавливает ВСЁ: ни одна
    # следующая правка не готовилась по чужому тексту.
    assert out["ops_applied"] == 0
    assert [r["reason"] for r in out["refused"]] == ["concurrent_edit"] * 2
    assert docs.batches == []


def test_retry_after_foreign_canary_rejection(engine, monkeypatch, tmp_path, capsys):
    """canary insert rejected by foreign edit -> attempt 1 has no base check."""
    docs = _twins(engine, monkeypatch)
    real_fresh = engine._fresh_anchor_snapshot
    calls = []

    def fresh(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            docs.rev = "R-foreign"   # someone edits; pinned canary rejected
            return None, "canary insert rejected (doc changed): x"
        return real_fresh(*a, **k)
    monkeypatch.setattr(engine, "_fresh_anchor_snapshot", fresh)
    code, out = _patch_with_base(engine, tmp_path, capsys,
        [{"op": "replace_quote", "quote": "Копия", "occurrence": 1, "with": "П1"}],
        _sidecar(engine, tmp_path, "R0"))
    assert out["ops_applied"] == 0
    assert out["refused"][0]["reason"] == "concurrent_edit"


def test_own_canary_in_no_live_probe_trips_base(engine, monkeypatch, tmp_path, capsys):
    docs = _twins(engine, monkeypatch)

    called = []

    def no_live(*a, **k):
        called.append(1)
        docs.rev = "R-own-canary"   # canary inserted and cleaned up by skrepka
        return None
    monkeypatch.setattr(engine, "_no_live_anchor_snapshot", no_live)
    code, out = _patch_with_base(engine, tmp_path, capsys,
        [{"op": "replace_quote", "quote": "Копия", "occurrence": 1, "with": "П1"},
         {"op": "replace_quote", "quote": "Копия", "occurrence": 2, "with": "П2"}],
        _sidecar(engine, tmp_path, "R0"))
    # Под `--base` доказательство «одни призраки» не берётся вовсе: своя
    # канарейка не должна читаться как чужая правка.
    assert called == []
    assert out["ops_applied"] == 2



def _docx_no_comments(first_text):
    def para(t): return f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"
    body = para("Первая") + para(first_text) + para("Вторая") + para("Другое")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def test_ghost_only_unproven_tab(engine, monkeypatch, tmp_path, capsys):
    threads = [_thread("ghost", "A", "2026-10-05T08:16:00Z")]
    _wire_export(engine, monkeypatch, _two_tab_doc("Alpha Omega"), threads,
                 _docx_no_comments("Совсем иное"))
    path = _ops_file(tmp_path, [{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}])
    try:
        engine.dry_run_patch("d1", path, tab_id="t.0")
    except SystemExit:
        pass
    out = json.loads(capsys.readouterr().out)
    verdict = out["operations"][0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "tab_boundary_unproven"


def test_dry_base_missing_reason(engine, monkeypatch, tmp_path, capsys):
    threads = [_thread("ghost", "A", "2026-10-05T08:16:00Z")]
    _wire_export(engine, monkeypatch, _two_tab_doc("Alpha Omega"), threads,
                 _docx_no_comments("Alpha Omega"))
    path = _ops_file(tmp_path, [{"op": "replace_quote", "quote": "Alpha", "with": "Beta"}])
    try:
        engine.dry_run_patch("d1", path, tab_id="t.0", base=str(tmp_path / "nope.json"))
    except SystemExit:
        pass
    out = json.loads(capsys.readouterr().out)
    # Сайдкара нет — это ошибка входа, а не «документ изменился».
    assert out["operations"][0]["reason"] == "schema_invalid"


def test_a_range_addressed_replace_consumes_no_quote_copy(engine):
    """Ревью r20, находка 6а: у операции с `range` лишние `quote` и
    `occurrence` не используются — копию цитаты она не уносит, и пересчёт
    номера по ней сдвинул бы соседнюю правку на чужую копию."""
    consumed = {}
    engine._note_consumed_copy(
        {"op": "replace_quote", "range": "r1", "quote": "Копия",
         "occurrence": 1, "with": "Другое"}, {}, consumed)
    assert consumed == {}
    engine._note_consumed_copy(
        {"op": "replace_quote", "quote": "Копия", "occurrence": 1,
         "text": "Копия!", "with": "Другое"}, {}, consumed)
    # писатель берёт `text` раньше `with`; цитата в нём осталась — не унесена
    assert consumed == {}
