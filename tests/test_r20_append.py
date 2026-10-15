"""`skrepka append` — дозапись блока с оформлением в конец вкладки.

Пост-мортем Авито 12.10: самая частая правка живого деливерабла жила в
самописном скрипте без тестов, и он сломался дважды — бесконечной рекурсией
и склейкой первого нового абзаца с последним старым («6+Подтверждение
участия» со стилем H2).
"""
import json

import pytest


def _para(start, text, style="NORMAL_TEXT", bullet=None, text_style=None):
    end = start + len(text) + 1
    el = {"startIndex": start, "endIndex": end, "paragraph": {
        "elements": [{"startIndex": start, "endIndex": end,
                      "textRun": {"content": text + "\n",
                                  "textStyle": text_style or {}}}],
        "paragraphStyle": {"namedStyleType": style}}}
    if bullet:
        el["paragraph"]["bullet"] = bullet
    return el


def _doc(paras, rev="R0", named=None):
    content, idx = [], 1
    for p in paras:
        text, style = (p, "NORMAL_TEXT") if isinstance(p, str) else p
        el = _para(idx, text, style)
        content.append(el)
        idx = el["endIndex"]
    doc = {"documentId": "d1", "revisionId": rev,
           "body": {"content": content}}
    if named:
        doc["namedRanges"] = named
    return doc


class _Docs:
    """Docs-сервис: чтение отдаёт `before`, после записи — `after`."""

    def __init__(self, before, after=None):
        self.before, self.after = before, after
        self.batches = []

    def documents(self):
        return self

    def get(self, **_):
        doc = self.after if (self.batches and self.after) else self.before
        return type("R", (), {"execute": lambda _s: doc})()

    def batchUpdate(self, documentId=None, body=None):
        self.batches.append(body)
        return type("R", (), {"execute": lambda _s: {}})()


def _wire(engine, monkeypatch, docs):
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: docs)


def _run(engine, tmp_path, capsys, md, **kw):
    path = tmp_path / "block.md"
    path.write_text(md, encoding="utf-8")
    code = 0
    try:
        engine.append_block("d1", str(path), **kw)
    except SystemExit as exc:
        code = exc.code
    return code, json.loads(capsys.readouterr().out)


# ------------------------------------------------------------- разбор ----

def test_the_block_markdown_is_read_with_its_formatting(engine):
    blocks, why = engine._append_blocks(
        "## Подтверждение участия\n\n"
        "**Вы записаны**  \nподзаголовок\n\n"
        "Текст с [ссылкой](https://ex.am/ple) и *курсивом*,\nдальше.\n")
    assert why is None
    assert [b["kind"] for b in blocks] == ["h2", "p", "p"]
    assert blocks[1]["text"] == "Вы записаны\vподзаголовок"
    assert blocks[1]["spans"] == [(0, 11, "bold")]
    assert blocks[2]["text"] == "Текст с ссылкой и курсивом, дальше."
    assert (8, 15, "link:https://ex.am/ple") in blocks[2]["spans"]
    assert (18, 26, "italic") in blocks[2]["spans"]


@pytest.mark.parametrize("md, word", [
    ("- пункт\n", "список"),
    ("1. пункт\n", "список"),
    ("| a | b |\n|---|---|\n| 1 | 2 |\n", "таблица"),
    ("> цитата\n", "цитата"),
    ("```\nкод\n```\n", "код"),
    ("текст с `кодом`\n", "code_inline"),
    ("![картинка](x.png)\n", "image"),
    ("\n\n", "нет ни одного"),
])
def test_what_append_cannot_place_is_refused_by_name(engine, md, word):
    blocks, why = engine._append_blocks(md)
    assert blocks is None and word in why


# --------------------------------------------------------------- план ----

def test_a_non_empty_tail_gets_a_leading_newline(engine):
    """Грабля 19.09 и 12.10: последний абзац «6+» непустой, и без ведущего
    перевода строки первый новый абзац приклеился бы к нему."""
    tab = _doc(["Старое", "6+"])
    blocks, _ = engine._append_blocks("## Заголовок\n\nТекст\n")
    plan, why = engine._append_plan(tab, blocks)
    assert why is None
    # «Старое\n» = [1, 8), «6+\n» = [8, 11): вставка перед его переводом строки
    assert plan["index"] == 10 and plan["lead"] is True
    assert plan["text"] == "\nЗаголовок\nТекст"
    assert (plan["block_start"], plan["block_end"]) == (11, 26)
    reqs = plan["requests"]
    assert reqs[0] == {"insertText": {"location": {"index": 10},
                                      "text": "\nЗаголовок\nТекст"}}
    styles = [(r["updateParagraphStyle"]["range"]["startIndex"],
               r["updateParagraphStyle"]["range"]["endIndex"],
               r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"])
              for r in reqs if "updateParagraphStyle" in r]
    assert styles == [(11, 21, "HEADING_2"), (21, 27, "NORMAL_TEXT")]
    # Ни один запрос, кроме самой вставки, не трогает старый абзац «6+».
    for r in reqs[1:]:
        rng = next(iter(r.values()))["range"]
        assert rng["startIndex"] >= plan["block_start"]


def test_an_empty_tail_becomes_the_first_new_paragraph(engine):
    tab = _doc(["Старое", ""])
    blocks, _ = engine._append_blocks("Текст\n")
    plan, _ = engine._append_plan(tab, blocks)
    assert plan["index"] == 8 and plan["lead"] is False
    assert plan["text"] == "Текст"
    assert (plan["block_start"], plan["block_end"]) == (8, 13)


def test_inherited_formatting_is_reset_not_inherited(engine):
    """Новый блок не наследует ни стиль абзаца, ни маркер списка, ни стиль
    текста последнего абзаца — от сноски, от списка, от полужирного."""
    tab = _doc(["Старое", "6+"])
    blocks, _ = engine._append_blocks("Текст **жирный**\n")
    plan, _ = engine._append_plan(tab, blocks)
    kinds = [next(iter(r)) for r in plan["requests"]]
    assert "deleteParagraphBullets" in kinds
    resets = [r["updateTextStyle"] for r in plan["requests"]
              if "updateTextStyle" in r and r["updateTextStyle"][
                  "textStyle"] == {}]
    assert resets and "bold" in resets[0]["fields"]
    para = [r["updateParagraphStyle"] for r in plan["requests"]
            if "updateParagraphStyle" in r][0]
    assert "indentStart" in para["fields"]
    bold = [r["updateTextStyle"] for r in plan["requests"]
            if r.get("updateTextStyle", {}).get("textStyle") == {
                "bold": True}]
    assert bold[0]["range"] == {"startIndex": 17, "endIndex": 23}


# ------------------------------------------------------- запись целиком ----

def _after_append():
    doc = _doc(["Старое", "6+"], rev="R1")
    content = doc["body"]["content"]
    content.append(_para(11, "Заголовок", "HEADING_2"))
    content.append(_para(21, "Текст"))
    return doc


def test_append_writes_one_pinned_batch_and_verifies(engine, monkeypatch,
                                                     tmp_path, capsys):
    docs = _Docs(_doc(["Старое", "6+"]), _after_append())
    _wire(engine, monkeypatch, docs)
    code, out = _run(engine, tmp_path, capsys, "## Заголовок\n\nТекст\n")
    assert code == 0, out
    [batch] = docs.batches
    assert batch["writeControl"] == {"requiredRevisionId": "R0"}
    assert out["action"] == "appended"
    assert out["verified"] == {"above_unchanged": True,
                               "block_matches": True}
    assert (out["block_start"], out["block_end"]) == (11, 26)


def test_a_restyled_old_tail_fails_the_verification(engine, monkeypatch,
                                                    tmp_path, capsys):
    """Текст выше точки вставки цел, а оформление старого хвоста поменялось —
    это тоже порча, и квитанция обязана её назвать (ревью Codex)."""
    after = _after_append()
    after["body"]["content"][1]["paragraph"]["paragraphStyle"] = {
        "namedStyleType": "HEADING_2"}
    docs = _Docs(_doc(["Старое", "6+"]), after)
    _wire(engine, monkeypatch, docs)
    code, out = _run(engine, tmp_path, capsys, "## Заголовок\n\nТекст\n")
    assert code == 3
    assert out["verified"]["above_unchanged"] is False


def test_a_wrong_block_style_fails_the_verification(engine, monkeypatch,
                                                    tmp_path, capsys):
    after = _after_append()
    after["body"]["content"][2]["paragraph"]["paragraphStyle"] = {
        "namedStyleType": "NORMAL_TEXT"}
    docs = _Docs(_doc(["Старое", "6+"]), after)
    _wire(engine, monkeypatch, docs)
    code, out = _run(engine, tmp_path, capsys, "## Заголовок\n\nТекст\n")
    assert code == 3
    assert out["verified"]["block_matches"] is False


def test_the_dry_run_writes_nothing(engine, monkeypatch, tmp_path, capsys):
    docs = _Docs(_doc(["Старое", "6+"]))
    _wire(engine, monkeypatch, docs)
    code, out = _run(engine, tmp_path, capsys, "Текст\n", dry_run=True)
    assert code == 0 and docs.batches == []
    assert out["writes_performed"] == 0 and out["leading_newline"] is True


def test_a_named_range_reaching_the_tail_stops_append(engine, monkeypatch,
                                                      tmp_path, capsys):
    named = {"Блок": {"namedRanges": [{"namedRangeId": "n1", "ranges": [
        {"startIndex": 8, "endIndex": 11}]}]}}
    docs = _Docs(_doc(["Старое", "6+"], named=named))
    _wire(engine, monkeypatch, docs)
    code, out = _run(engine, tmp_path, capsys, "Текст\n")
    assert code == 1
    assert out["reason"] == "named_range_overlap"
    assert docs.batches == []


def test_a_stale_base_stops_append(engine, monkeypatch, tmp_path, capsys):
    docs = _Docs(_doc(["Старое", "6+"]))
    _wire(engine, monkeypatch, docs)
    base = tmp_path / "x.md.gdocs-base.json"
    base.write_text(json.dumps({
        "schema_version": engine.SIDECAR_SCHEMA_VERSION, "doc_id": "d1",
        "revision_id": "R-old", "elements": []}), encoding="utf-8")
    code, out = _run(engine, tmp_path, capsys, "Текст\n", base=str(base))
    assert code == 1 and out["reason"] == "concurrent_edit"
    assert docs.batches == []
