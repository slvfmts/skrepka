"""Оглавление огораживается по месту, а не запирает документ (#23, r19/T4).

До 0.18 само наличие `tableOfContents` во вкладке выключало перезапись
прокомментированного фрагмента во ВСЁМ документе. Причина была писательская:
запись шла поиском по тексту, который заходит и в оглавление, а счёт вхождений
туда не смотрел — обходчики текста оглавление не читают. Поиска больше нет,
значит и глобального отказа быть не должно.

Здесь проверяется то, что осталось: документ с оглавлением правится целиком,
адрес в само оглавление ненаходим ни цитатой, ни диапазоном, а ограда по
пересечению — страховка, чей текст всё равно обязан соответствовать рамке.
"""
import json

import pytest

from test_sync_anchors import (
    CREATED,
    CREATED_SEC,
    DocsStub,
    DriveStub,
    _docx_builder,
    api_comment,
    make_doc,
    wire,
)

TEXTS = ["Alpha", "Bravo", "Charlie"]


def _toc(start, end):
    return {"startIndex": start, "endIndex": end, "tableOfContents": {}}


def _stand(engine, monkeypatch, toc_at=None):
    """Документ Alpha/Bravo/Charlie с якорем на «Bravo» и оглавлением."""
    doc = make_doc(TEXTS)
    if toc_at is not None:
        doc["body"]["content"].append(_toc(*toc_at))
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []), ("Bravo", [("0", 0, 5)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    return docs, drive


def _run(engine, tmp_path, ops):
    path = tmp_path / "ops.json"
    path.write_text(json.dumps(ops), encoding="utf-8")
    try:
        engine.patch_doc("doc1", str(path))
        return 0
    except SystemExit as exc:
        return exc.code


def test_intervals_read_only_well_formed_entries(engine):
    """Элемент без индексов огородить нечем: он не даёт координат, а гадать
    по нему — это ровно тот глобальный отказ, от которого уходим."""
    tab = {"body": {"content": [
        _toc(10, 20),
        {"tableOfContents": {}},                      # без индексов
        {"startIndex": 40, "endIndex": 40, "tableOfContents": {}},  # пустой
        {"paragraph": {"elements": []}},
    ]}}
    assert engine._table_of_contents_intervals(tab) == [(10, 20, "оглавление")]


def test_a_document_with_a_table_of_contents_is_editable(engine, monkeypatch,
                                                         tmp_path):
    """Главное, ради чего задача: документ с оглавлением перестал быть
    нередактируемым. Правка накрывает якорь целиком — раньше это был отказ
    просто потому, что где-то в файле есть оглавление."""
    docs, _drive = _stand(engine, monkeypatch, toc_at=(900, 950))
    code = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}])
    assert code == 0


def test_every_operation_still_applies_with_a_table_of_contents(engine,
                                                               monkeypatch,
                                                               tmp_path,
                                                               capsys):
    """Локальность в её рабочем виде: оглавление в документе есть, и ни одна
    правка от этого не страдает — включая ту, что накрывает якорь целиком.
    До 0.18 обе отказали бы."""
    _docs, _drive = _stand(engine, monkeypatch, toc_at=(900, 950))
    code = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"},
        {"op": "replace_quote", "quote": "Charlie", "with": "Xray"},
    ])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ops_applied"] == 2
    assert not out.get("refused")


def test_the_refusal_does_not_send_the_person_to_do_it_by_hand(engine):
    """Продуктовая рамка: автор не просит редактора сделать техническую
    работу за него. Совет по оглавлению — редакторский, а не про интерфейс.

    Проверяется на самом тексте отказа, а не через весь путь: адрес, который
    реально попадает в оглавление, документ Google произвести не может —
    цитата туда не резолвится, а именованный диапазон отклоняется раньше как
    несплошной. Ограда по пересечению — страховка, и её текст всё равно
    обязан соответствовать рамке."""
    from test_sync_anchors import _rewrite_case

    kw = _rewrite_case(engine)
    kw["doc_tab"]["body"]["content"].append(_toc(kw["start"], kw["end"]))
    why = engine._why_no_rewrite(
        doc_tab=kw["doc_tab"], search_text=kw["search_text"],
        new_text=kw["new_text"], start=kw["start"], end=kw["end"],
        anchors=kw["anchors"], attribution=kw["attribution"],
        named_intervals=[])
    assert "оглавление" in why and "заголовок" in why
    for forbidden in ("в интерфейсе", "руками", "в UI"):
        assert forbidden not in why


def test_the_contents_text_is_not_addressable_at_all(engine):
    """Цитата внутри оглавления ненаходима: обходчики текста читают абзацы и
    таблицы, а оглавление — ни то ни другое. Значит адресовать туда правку
    нечем, и отказ по пересечению остаётся страховкой, а не рабочим путём."""
    doc = make_doc(TEXTS)
    doc["body"]["content"].append({
        "startIndex": 900, "endIndex": 950,
        "tableOfContents": {"content": [
            {"paragraph": {"elements": [
                {"startIndex": 901, "endIndex": 910,
                 "textRun": {"content": "Оглавное\n"}}]}}]}})
    tab = doc
    buf, _imap = engine._text_buffer(tab)
    assert "Оглавное" not in buf
    assert engine._find_quote_in_doctab(tab, "Оглавное") is None


def test_a_named_range_crossing_the_contents_is_refused_as_non_contiguous(
        engine):
    """Именованный диапазон — единственный адрес, который в принципе мог бы
    перешагнуть оглавление. Он отклоняется раньше, на несплошном покрытии
    текстовыми ранами, и это верный ответ."""
    doc = make_doc(TEXTS)
    doc["body"]["content"].append(_toc(900, 950))
    # диапазон, который тянется через оглавление
    assert engine._extract_exact_text_range(doc, 1, 960) is None


@pytest.mark.parametrize("toc_at,expected", [
    ((900, 950), False),   # в стороне — не причина
    (None, False),         # оглавления нет вовсе
])
def test_a_distant_contents_is_never_the_named_reason(engine, toc_at,
                                                      expected):
    from test_sync_anchors import _rewrite_case

    kw = _rewrite_case(engine)
    if toc_at is not None:
        kw["doc_tab"]["body"]["content"].append(_toc(*toc_at))
    why = engine._why_no_rewrite(
        doc_tab=kw["doc_tab"], search_text=kw["search_text"],
        new_text=kw["new_text"], start=kw["start"], end=kw["end"],
        anchors=kw["anchors"], attribution=kw["attribution"],
        named_intervals=[])
    assert ("оглавление" in why) is expected
