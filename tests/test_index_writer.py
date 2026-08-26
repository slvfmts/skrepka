"""0.17: адресация по индексу вместо поиска по тексту.

Два случая из пост-мортема 25.08.2026, оба стоили перезалива документа и
уничтоженных тредов:

1. Два превью рассылки совпадают символ в символ — формат CRM-деливерабла
   этого требует. Адресовать было нечем: цитата неуникальна, расширить её
   нельзя, `occurrence` на комментированном документе запрещали, а
   именованный диапазон приносил точные индексы, которые тут же выбрасывали
   и снова искали по тексту.
2. Из шапки «Задача | Бриф» нельзя было убрать ссылку: кусок разностилевой,
   и `replaceAllText` схлопнул бы стили. Но это удаление — оформлять нечего.
"""
import pytest

from test_sync_anchors import (
    CREATED,
    CREATED_SEC,
    DocsStub,
    DriveStub,
    _docx_builder,
    api_comment,
    make_doc,
    semantic_batch,
    wire,
    written_text,
)

LINK = {"link": {"url": "https://example.test"}, "underline": True}

DUP = "Спрос растёт — в «Можно продать» ждут одежда, обувь и настольные игры"
NEW = "Спрос растёт — в «Можно продать» ждут одежда и обувь"


def _doc_runs(paras, rev="R0"):
    """Документ, где абзац задан списком (текст, стиль) — по прогону на пару."""
    content, idx = [], 1
    for runs in paras:
        if isinstance(runs, str):
            runs = [(runs, {})]
        text = "".join(x for x, _s in runs)
        s, e = idx, idx + len(text) + 1
        elements, pos = [], s
        for k, (x, style) in enumerate(runs):
            piece = x + ("\n" if k == len(runs) - 1 else "")
            elements.append({"startIndex": pos, "endIndex": pos + len(piece),
                             "textRun": {"content": piece,
                                         "textStyle": dict(style)}})
            pos += len(piece)
        content.append({"startIndex": s, "endIndex": e, "paragraph": {
            "elements": elements,
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}})
        idx = e
    return {"documentId": "doc1", "revisionId": rev,
            "body": {"content": content}}


def _stand(engine, monkeypatch, doc, paras, anchors_para, anchor_span):
    """Документ с одним живым якорем на указанном абзаце."""
    docs = DocsStub(doc)
    export = [(p, [("0", *anchor_span)] if i == anchors_para else [])
              for i, p in enumerate(paras)]
    drive = DriveStub([api_comment("c1", "A", CREATED)],
                      _docx_builder(docs, export, [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    return docs, drive


# ---------------------------------------------------------------------------
# случай 1: одинаковые абзацы
# ---------------------------------------------------------------------------

def test_duplicate_paragraph_is_addressable_by_occurrence(engine, monkeypatch):
    """Главный боевой случай. Раньше отказ, теперь правится названная копия."""
    docs, drive = _stand(engine, monkeypatch,
                         make_doc(["Заголовок", DUP, "Хвост", DUP]),
                         ["Заголовок", DUP, "Хвост", DUP], 0, (0, 9))
    note = engine._apply_op_anchor_safe(docs, drive, "doc1", {
        "op": "replace_quote", "quote": DUP, "with": NEW,
        "occurrence": 2}, None)
    assert note is None or note.get("applied_as") != "no-op"
    main = semantic_batch(docs)
    assert not any("replaceAllText" in r for r in main)
    assert written_text(main) == NEW
    # вторая копия начинается после «Заголовок\n» и первой копии
    cut = next(r["deleteContentRange"]["range"] for r in main[1:]
               if "deleteContentRange" in r)
    assert cut["startIndex"] == 1 + len("Заголовок") + 1 + len(DUP) + 1 \
        + len("Хвост") + 1


def test_first_copy_is_untouched_when_the_second_is_addressed(engine,
                                                              monkeypatch):
    """Соседняя копия не должна измениться — этого и не умел replaceAllText."""
    docs, drive = _stand(engine, monkeypatch,
                         make_doc([DUP, DUP, "Хвост"]),
                         [DUP, DUP, "Хвост"], 2, (0, 5))
    engine._apply_op_anchor_safe(docs, drive, "doc1", {
        "op": "replace_quote", "quote": DUP, "with": NEW,
        "occurrence": 2}, None)
    main = semantic_batch(docs)
    cut = next(r["deleteContentRange"]["range"] for r in main[1:]
               if "deleteContentRange" in r)
    assert cut["startIndex"] == 1 + len(DUP) + 1      # ровно вторая копия
    assert cut["endIndex"] == cut["startIndex"] + len(DUP)


def test_ambiguous_quote_without_occurrence_still_refuses(engine, monkeypatch):
    """Скрепка по-прежнему не выбирает копию за человека — но совет теперь
    называет путь, который работает."""
    docs, drive = _stand(engine, monkeypatch,
                         make_doc([DUP, DUP, "Хвост"]),
                         [DUP, DUP, "Хвост"], 2, (0, 5))
    with pytest.raises(SystemExit):
        engine._apply_op_anchor_safe(docs, drive, "doc1", {
            "op": "replace_quote", "quote": DUP, "with": NEW}, None)


def test_named_range_on_a_duplicate_is_an_address_not_a_search(engine,
                                                               monkeypatch):
    """`mark` для того и заводят: он снимает неоднозначность. Раньше patch
    получал его индексы и всё равно требовал уникальности текста."""
    doc = make_doc([DUP, DUP, "Хвост"])
    doc["namedRanges"] = {"nc3pre": {"namedRanges": [{
        "namedRangeId": "nr1",
        "ranges": [{"startIndex": 1 + len(DUP) + 1,
                    "endIndex": 1 + len(DUP) + 1 + len(DUP)}]}]}}
    docs, drive = _stand(engine, monkeypatch, doc,
                         [DUP, DUP, "Хвост"], 2, (0, 5))
    engine._apply_op_anchor_safe(docs, drive, "doc1", {
        "op": "replace_range", "range": "nc3pre", "text": NEW}, None)
    main = semantic_batch(docs)
    assert written_text(main) == NEW
    cut = next(r["deleteContentRange"]["range"] for r in main[1:]
               if "deleteContentRange" in r)
    assert cut["startIndex"] == 1 + len(DUP) + 1


# ---------------------------------------------------------------------------
# случай 2: удаление через границу оформления
# ---------------------------------------------------------------------------

def test_deleting_across_a_style_boundary_is_allowed(engine, monkeypatch):
    """Шапка «Задача | Бриф»: убрать ссылку. Удалению нечего оформлять."""
    doc = _doc_runs([[("Задача", {}), (" | ", {}), ("Бриф", LINK)],
                     "Тело письма"])
    docs, drive = _stand(engine, monkeypatch, doc,
                         ["Задача | Бриф", "Тело письма"], 0, (0, 6))
    engine._apply_op_anchor_safe(docs, drive, "doc1", {
        "op": "replace_quote", "quote": " | Бриф", "with": ""}, None)
    main = semantic_batch(docs)
    assert written_text(main) == ""            # чистое удаление
    assert not any("insertText" in r for r in main)
    assert not any("updateTextStyle" in r for r in main)
    cut = next(r["deleteContentRange"]["range"] for r in main[1:]
               if "deleteContentRange" in r)
    assert cut == {"startIndex": 1 + len("Задача"),
                   "endIndex": 1 + len("Задача | Бриф")}


def test_non_empty_replacement_narrows_to_one_style_when_it_can(engine,
                                                                monkeypatch):
    """Замена — другое дело: у нового текста должно быть одно оформление.
    Если менять надо только обычный текст, правка ужимается до него, а
    ссылка рядом остаётся нетронутой."""
    doc = _doc_runs([[("Задача", {}), (" | ", {}), ("Бриф", LINK)],
                     "Тело письма"])
    docs, drive = _stand(engine, monkeypatch, doc,
                         ["Задача | Бриф", "Тело письма"], 0, (0, 6))
    note = engine._apply_op_anchor_safe(docs, drive, "doc1", {
        "op": "replace_quote", "quote": " | Бриф", "with": " и Бриф"}, None)
    assert note["applied_as"] == "narrowed"
    assert note["narrowed_to"] == " | "
    main = semantic_batch(docs)
    assert written_text(main) == " и "
    cut = next(r["deleteContentRange"]["range"] for r in main[1:]
               if "deleteContentRange" in r)
    assert cut == {"startIndex": 1 + len("Задача"),
                   "endIndex": 1 + len("Задача | ")}


def test_non_empty_replacement_across_styles_refuses_when_narrowing_cannot(
        engine, monkeypatch):
    """Когда изменения расползлись по обе стороны границы оформления, сузить
    некуда: какое из двух оформлений отдать новому тексту, скрепка решать не
    вправе, и это по-прежнему отказ."""
    doc = _doc_runs([[("Задача", {}), (" | ", {}), ("Бриф", LINK)],
                     "Тело письма"])
    docs, drive = _stand(engine, monkeypatch, doc,
                         ["Задача | Бриф", "Тело письма"], 0, (0, 6))
    with pytest.raises(SystemExit):
        engine._apply_op_anchor_safe(docs, drive, "doc1", {
            "op": "replace_quote", "quote": " | Бриф", "with": " / Задание"},
            None)


def test_replacement_states_the_style_instead_of_inheriting_it(engine,
                                                               monkeypatch):
    """`insertText` наследует оформление от соседа ненадёжно, поэтому стиль
    задаётся явно — иначе новый текст рядом со ссылкой может стать ссылкой."""
    doc = _doc_runs([[("Тело ", {}), ("ссылка", LINK), (" хвост", {})],
                     "Второй абзац"])
    docs, drive = _stand(engine, monkeypatch, doc,
                         ["Тело ссылка хвост", "Второй абзац"], 1, (0, 6))
    engine._apply_op_anchor_safe(docs, drive, "doc1", {
        "op": "replace_quote", "quote": "ссылка", "with": "адрес"}, None)
    main = semantic_batch(docs)
    style = next(r["updateTextStyle"] for r in main if "updateTextStyle" in r)
    assert style["textStyle"]["link"] == {"url": "https://example.test"}
    assert style["range"]["endIndex"] - style["range"]["startIndex"] == \
        len("адрес")


# ---------------------------------------------------------------------------
# главный боевой случай: дубль, на котором висит живой комментарий
# ---------------------------------------------------------------------------

def test_the_commented_copy_itself_is_editable(engine, monkeypatch):
    """Постскриптум пост-мортема: «на одном из этих двух вхождений висит мой
    собственный свежий комментарий — замечание, которое я оставил, чтобы его
    отработали, отработать нельзя».

    Теперь можно. Копия, на которой стоит якорь, доказывается местом в
    выгрузке, а сам якорь переписывается изнутри, поэтому тред переезжает на
    новый текст вместо того, чтобы стать призраком."""
    docs, drive = _stand(engine, monkeypatch,
                         make_doc(["Заголовок", DUP, "Хвост", DUP]),
                         ["Заголовок", DUP, "Хвост", DUP], 3, (0, len(DUP)))
    note = engine._apply_op_anchor_safe(docs, drive, "doc1", {
        "op": "replace_quote", "quote": DUP, "with": NEW,
        "occurrence": 2}, None)
    assert note["applied_as"] == "rewritten"
    main = semantic_batch(docs)
    # правка идёт по второй копии: она начинается после «Заголовок», первой
    # копии и «Хвост»
    second = 1 + len("Заголовок") + 1 + len(DUP) + 1 + len("Хвост") + 1
    assert any(r.get("insertText", {}).get("location", {}).get("index",
                                                               -1) >= second
               for r in main)
    # промежуточный поиск склеивает старый текст с новым, поэтому в нетронутой
    # первой копии такой строки нет и она не может быть задета
    probe = next(r["replaceAllText"]["containsText"]["text"] for r in main
                 if "replaceAllText" in r)
    assert NEW in probe and probe not in DUP
    assert probe.count(DUP[:20]) >= 1


def test_the_free_copy_stays_untouched_while_the_commented_one_changes(
        engine, monkeypatch):
    """Соседняя копия не должна пострадать — ради этого всё и затевалось."""
    docs, drive = _stand(engine, monkeypatch,
                         make_doc([DUP, DUP]), [DUP, DUP], 1, (0, len(DUP)))
    engine._apply_op_anchor_safe(docs, drive, "doc1", {
        "op": "replace_quote", "quote": DUP, "with": NEW,
        "occurrence": 2}, None)
    main = semantic_batch(docs)
    for r in main[1:]:
        rng = r.get("deleteContentRange", {}).get("range")
        if rng and rng["endIndex"] <= 1 + len(DUP) + 1:
            raise AssertionError(f"правка задела первую копию: {rng}")
