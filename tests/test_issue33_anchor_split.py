"""Правка стирает прокомментированное слово, разговор садится на соседнее
(#33, r19/T9).

`replace_anchor` переписывает якорь непустым текстом. Если слово надо убрать
вовсе, якорь схлопывается в ноль и разговор уходит в историю комментариев —
для человека он просто пропадает из панели. Правило владельца: посадить якорь
на СОСЕДНЕЕ слово.

Контракт без единого числа: вызывающий называет видимый старый внешний текст и
новый по обе стороны, итог — `before + after`. Всё остальное считает движок.
Композиция трёх частей в одном батче замерена в `internal/MEASURE-M30.md`;
прежняя реализация на `sprint/public-mvp` требовала смещений в UTF-16 от
вызывающего и не переносится.
"""
import json

import pytest

from test_sync_anchors import (
    CREATED,
    CREATED_SEC,
    DocsStub,
    DriveStub,
    MutatingDocsStub,
    _docx_builder,
    api_comment,
    make_doc,
    semantic_batch,
    wire,
)

PARA = "Мы обсудили ЛИШНЕЕ слово подробно"
A_OFF = (12, 18)                      # «ЛИШНЕЕ» в смещениях абзаца
A_S, A_E = 1 + A_OFF[0], 1 + A_OFF[1]  # ...и в индексах Docs: тело с единицы
O_S, O_E = 1, 1 + len(PARA)


@pytest.fixture
def raising(engine):
    """Прямой вызов внутренностей: отказ обязан быть исключением, не exit."""
    engine._RAISE_ERRORS = True
    try:
        yield engine
    finally:
        engine._RAISE_ERRORS = False


def _tab(texts=(PARA,), named_ranges=None):
    return make_doc(list(texts), named_ranges=named_ranges)


def _snap(anchors=None, attribution=None, blocked=(), places=None):
    anchors = (anchors if anchors is not None
               else [(A_S, A_E, "ЛИШНЕЕ", "0")])
    return {"anchors": anchors,
            "attribution": (attribution if attribution is not None
                            else {"0": "c1"}),
            "blocked": list(blocked),
            "thread_places": places if places is not None else {"c1": 1}}


def _op(before="Мы кратко обсудили ", after="слово подробно", quote=PARA,
        cid="c1"):
    return {"op": "replace_around_anchor", "comment_id": cid,
            "quote": quote, "with": {"before": before, "after": after}}


def _plan(engine, op=None, tab=None, snap=None, named=()):
    tab = tab if tab is not None else _tab()
    snap = snap if snap is not None else _snap()
    r = engine._resolve_around_target(op or _op(), tab, snap, None, "doc1")
    return r, engine._replace_around_plan(tab, r, snap, list(named),
                                          closed_present=False)


# ------------------------------------------------------------- носитель ----

@pytest.mark.parametrize("text, want", [
    ("слово", [(0, 5)]),
    ("из-за", [(0, 5)]),                    # дефис между буквами склеивает
    ("по‐русски", [(0, 9)]),           # настоящий U+2010 — тоже
    ("3-летний", [(0, 8)]),                 # соединитель между цифрой и буквой
    ("l’esprit", [(0, 8)]),            # апостроф внутри слова
    ("слово-", [(0, 5)]),                   # висячий дефис в слово не входит
    ("а — б", [(0, 1), (4, 5)]),            # тире не соединитель
    ("да нет", [(0, 2), (3, 6)]),      # неразрывный пробел разделяет
    ("_x_", [(1, 2)]),                      # подчёркивание словом не делает
    ("е\u0301ль", [(0, 4)]),           # комбинирующий знак — часть слова
])
def test_word_boundaries_follow_the_stated_rule(engine, text, want):
    assert engine._lexical_word_spans(text) == want


@pytest.mark.parametrize("text", [
    "ʼ",          # одиночный Lm: апостроф-модификатор не слово
    "́̂",    # только комбинирующие знаки — стержня нет
    "😀 🎉",           # эмодзи не слово
    "   ",             # пробелы
    "— … !",           # пунктуация
])
def test_text_without_a_word_yields_no_carrier(engine, text):
    span, why = engine._carrier_span(text, first=True)
    assert span is None and why == "no_word"


@pytest.mark.parametrize("text, why", [
    ("漢字 текст", "unsegmented_script"),     # письменность без пробелов
    ("ทดสอบ", "unsegmented_script"),
    ("1️⃣ дальше", "grapheme_cluster"),   # keycap начинается с цифры
    ("؀буква", "grapheme_cluster"),      # Prepend перед буквой
    ("а‍б", "grapheme_cluster"),         # ZWJ внутри
])
def test_candidates_we_cannot_cut_safely_are_refused(engine, text, why):
    """Отказ по НАСТОЯЩЕЙ причине, а не разрез на удачу.

    Довод «границы безопасны по построению» неверен, и ревью привело эти
    контрпримеры: `Prepend` режет кластер слева, ZWJ — справа, а keycap
    начинается с цифры и прошёл бы как слово вопреки правилу «эмодзи
    носителем не становится».
    """
    span, got = engine._carrier_span(text, first=True)
    assert span is None and got == why


def test_the_right_neighbour_wins_and_the_left_is_the_fallback(engine):
    right, _ = engine._around_carrier_split("до ", "после и хвост")
    assert right["carrier"] == "после" and right["side"] == "after"
    left, _ = engine._around_carrier_split("хвост длинный", " — ")
    assert left["carrier"] == "длинный" and left["side"] == "before"


def test_no_word_on_either_side_is_infeasible(engine):
    split, why = engine._around_carrier_split(" — ", " … ")
    assert split is None and why == "no_word"


def test_the_cut_is_positional_so_a_leading_space_is_not_lost(engine):
    """Носитель не обязан стоять с краю своей вставки.

    Строковый разрез («убрать слово из `after`») потерял бы пробел перед ним
    или приклеил бы его к носителю. Позиционный отдаёт всё, что стоит ДО
    носителя, левой вставке — и итог остаётся ровно `before + after`.
    """
    split, _ = engine._around_carrier_split("Тут", " длиннее и хвост")
    assert split["carrier"] == "длиннее"
    assert split["left"] == "Тут "
    assert split["right"] == " и хвост"
    assert split["left"] + split["carrier"] + split["right"] == \
        "Тут" + " длиннее и хвост"


# ----------------------------------------------------------------- схема ---

def test_the_schema_takes_no_numbers_at_all(raising):
    """Главное отличие от прежней реализации: вызывающий не считает смещений.

    На `sprint/public-mvp` контракт требовал `before_utf16`, `after_utf16` и
    `anchor.start_utf16`. Считать индексы Docs в UTF-16 руками — работа, в
    которой ошибётся и человек, и агент, и ошибка выйдет порчей текста.
    """
    cid, quote, before, after = raising._validate_around_op(_op())
    assert (cid, quote) == ("c1", PARA)
    assert (before, after) == ("Мы кратко обсудили ", "слово подробно")


@pytest.mark.parametrize("op, need", [
    ({"op": "replace_around_anchor", "quote": PARA,
      "with": {"before": "a", "after": "b"}}, "comment_id"),
    ({"op": "replace_around_anchor", "comment_id": "c1",
      "with": {"before": "a", "after": "b"}}, "quote"),
    ({"op": "replace_around_anchor", "comment_id": "c1", "quote": PARA,
      "with": "строка"}, "'with'"),
    ({"op": "replace_around_anchor", "comment_id": "c1", "quote": PARA,
      "with": {"before": "", "after": ""}}, "пусты"),
    ({"op": "replace_around_anchor", "comment_id": "c1", "quote": PARA,
      "with": {"before": "a\nb", "after": ""}}, "перевод строки"),
])
def test_a_schema_error_looks_like_a_schema_error(raising, op, need):
    """Опечатка обязана выглядеть опечаткой, а не «цель не разрешилась»:
    у поздней операции разрешение цели штатно откладывается, и настоящие
    ошибки входа потерялись бы среди него."""
    with pytest.raises(raising.PatchOpError) as exc:
        raising._validate_around_op(op)
    assert need in str(exc.value)


# ------------------------------------------------------------- адресация ---

def test_the_quote_witnesses_the_place_but_the_thread_is_the_address(raising):
    """Цитата здесь не адрес, а свидетель границ: повторись она в другом
    абзаце — правится тот, где стоит комментарий."""
    tab = _tab([PARA, PARA])
    r, _p = _plan(raising, tab=tab, snap=_snap())
    assert (r["start"], r["end"]) == (O_S, O_E)
    assert (r["anchor_start"], r["anchor_end"]) == (A_S, A_E)


def test_a_quote_that_does_not_cover_the_comment_is_refused(raising):
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, op=_op(quote="подробно"))
    assert exc.value.reason == "quote_not_found"
    assert "не охватывает место комментария" in str(exc.value)


def test_a_quote_covering_the_comment_twice_is_refused(raising):
    """Неоднозначность — отказ, а не догадка: выбирать копию за человека
    скрепка не станет ни при какой адресации.

    Форма редкая, но настоящая: цитата, вхождения которой ПЕРЕКРЫВАЮТСЯ, может
    содержать один и тот же комментарий дважды.
    """
    tab = _tab(["ааа"])
    snap = _snap(anchors=[(2, 3, "а", "0")])
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, op=_op(before="б", after="в", quote="аа"),
              tab=tab, snap=snap)
    assert exc.value.reason == "quote_ambiguous"
    assert "не станет выбирать за вас" in str(exc.value)


def test_a_thread_without_a_placed_anchor_is_unresolvable(raising):
    snap = _snap(places={"c1": 2})
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, snap=snap)
    assert exc.value.reason == "comment_thread_has_multiple_anchors"


# -------------------------------------------------------------- геометрия --

def _kinds(reqs):
    return [next(iter(r)) for r in reqs]


def test_the_order_of_the_six_steps_is_the_measured_one(raising):
    """Порядок — семантический инвариант, а не деталь реализации.

    Вставка строго внутрь якоря им поглощается, на границе — нет (M28), значит
    порядок решает, где окажется разговор. Контроль в M30: тот же батч с
    обратным порядком дал «Мы кратко обсудил подробноодробно» и убил якорь.
    """
    _r, (reqs, _split, _parts) = _plan(raising)
    assert _kinds(reqs) == [
        "insertText",           # 1. носитель строго внутрь якоря
        "deleteContentRange",   # 2. старая голова, исходные координаты
        "deleteContentRange",   # 3. уцелевший хвост
        "updateTextStyle",      # 4. носитель — ПОСЛЕ обоих удалений
        "deleteContentRange",   # 5. правая часть в сдвинутых координатах
        "insertText",
        "updateTextStyle",
        "deleteContentRange",   # 6. левая часть в исходных координатах
        "insertText",
        "updateTextStyle",
    ]


def test_the_style_mask_lands_on_the_carrier_not_on_the_old_head(raising):
    """Маска стоит после ОБОИХ удалений.

    Поставь её сразу после вставки носителя — она легла бы на промежуточный
    диапазон, где ещё сидит старая голова, а носитель унаследовал бы стиль
    соседа (`insertText` наследует его — правка 0.16). Замерено M30-A2.
    """
    _r, (reqs, split, _p) = _plan(raising)
    mask = reqs[3]["updateTextStyle"]["range"]
    assert mask == {"startIndex": A_S,
                    "endIndex": A_S + len(split["carrier"])}


def test_the_right_side_moves_by_the_anchor_delta_and_the_left_does_not(
        raising):
    _r, (reqs, split, _p) = _plan(raising)
    d_a = len(split["carrier"]) - (A_E - A_S)
    assert reqs[4]["deleteContentRange"]["range"] == {
        "startIndex": A_E + d_a, "endIndex": O_E + d_a}
    assert reqs[7]["deleteContentRange"]["range"] == {
        "startIndex": O_S, "endIndex": A_S}


def test_the_three_parts_still_spell_before_plus_after(raising):
    _r, (reqs, split, _p) = _plan(raising)
    assert split["left"] + split["carrier"] + split["right"] == \
        "Мы кратко обсудили " + "слово подробно"
    # запросы идут в порядке БАТЧА (носитель, правая, левая), а не в порядке
    # чтения: порядок здесь семантический инвариант, а не оформление
    assert sorted(q["insertText"]["text"] for q in reqs if "insertText" in q) \
        == sorted([split["left"], split["carrier"], split["right"]])


def test_an_empty_old_side_still_writes_its_new_text(raising):
    """Удаление и вставка порождаются НЕЗАВИСИМО.

    Якорь стоит в конце фрагмента, старой правой стороны нет вовсе, а новая
    непуста. Условие по ширине СТАРОЙ стороны потеряло бы половину результата
    (находка ревью, круг 2; замерено M30-B).
    """
    para = "Итог зависит от ЛИШНЕГО"
    tab = _tab([para])
    a_s, a_e = 1 + 16, 1 + 23
    snap = _snap(anchors=[(a_s, a_e, "ЛИШНЕГО", "0")])
    op = _op(before="Итог зависит от контекста", after=" — ", quote=para)
    _r, (reqs, split, _p) = _plan(raising, op=op, tab=tab, snap=snap)
    assert split["carrier"] == "контекста" and split["right"] == " — "
    right = [q for q in reqs[4:] if "insertText" in q]
    assert right[0]["insertText"]["text"] == " — "
    # удаления справа нет — удалять нечего, а вставка есть
    assert not [q for q in reqs[4:]
                if "deleteContentRange" in q
                and q["deleteContentRange"]["range"]["startIndex"] >= a_e]


def test_a_part_that_changes_nothing_is_not_written_at_all(raising):
    """Тождественная замена сначала удаляет, потом вставляет — и удаление
    убило бы чужой тред внутри этой части ради записи того же текста.

    Это же закрывает обычный случай «убери слово, остальное оставь»."""
    op = _op(before="Мы обсудили ", after="слово подробно")
    _r, (reqs, _split, parts) = _plan(raising, op=op)
    assert [p["kind"] for p in parts] == ["anchor", "right"]
    assert all(q["deleteContentRange"]["range"]["startIndex"] >= A_S
               for q in reqs if "deleteContentRange" in q)


def test_nothing_to_write_is_an_honest_zero(raising):
    """Ноль ставится по ФАКТИЧЕСКИМ частям, а не по равенству текста:
    тот же текст бывает получен другим разрезом, и тогда комментарий
    переезжает — это работа, а не ноль."""
    op = _op(before="Мы обсудили ", after="ЛИШНЕЕ слово подробно")
    _r, (reqs, _split, parts) = _plan(raising, op=op)
    assert reqs is None and parts == []


# ----------------------------------------------------------------- ограды --

def test_a_foreign_thread_wiped_by_the_composition_is_refused(raising):
    """Композиционная дыра: чужой якорь целиком внутри стираемой части.

    Ни одна отдельная часть его не «накрывает целиком» в смысле обычной
    замены — накрывает объединение фактических удалений.
    """
    snap = _snap(anchors=[(A_S, A_E, "ЛИШНЕЕ", "0"), (4, 12, "обсудили", "1")],
                 attribution={"0": "c1", "1": "c2"})
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, snap=snap)
    assert exc.value.reason == "comment_anchor_would_be_lost"
    assert "пропал бы из панели" in str(exc.value)


def test_a_foreign_thread_inside_an_UNWRITTEN_part_survives(raising):
    """Обречённость считается по ФАКТИЧЕСКИМ удалениям, а не по внешнему
    диапазону.

    Левая часть тождественна и не пишется вовсе, значит чужой тред внутри неё
    операцией не задет. Правило «любой чужой якорь внутри цитаты — отказ»
    отказало бы здесь по живому документу. Тест держит ровно эту разницу:
    мутант, считающий обречённость по внешнему диапазону, отказывает, а
    правильный код применяет правку.
    """
    snap = _snap(anchors=[(A_S, A_E, "ЛИШНЕЕ", "0"), (4, 12, "обсудили", "1")],
                 attribution={"0": "c1", "1": "c2"})
    op = _op(before="Мы обсудили ", after="фраза подробно")
    _r, (reqs, _split, parts) = _plan(raising, op=op, snap=snap)
    assert reqs and [p["kind"] for p in parts] == ["anchor", "right"]


def test_a_thread_dies_only_when_its_LAST_anchor_goes(raising):
    """Смысл тот же, что у `_doomed_threads`: тред живёт, пока жив хоть один
    его якорь. Геометрию «два якоря по разные стороны» импортом docx
    воспроизвести не удалось (проба M30-E), поэтому она держится здесь."""
    dels = [(O_S, A_S), (A_E, O_E)]
    both = [(4, 12, "обсудили", "1"), (19, 24, "слово", "2")]
    assert raising._around_doomed(dels, both, {"1": "c2", "2": "c2"}, "c1")
    one_outside = [(4, 12, "обсудили", "1"), (99, 104, "поодаль", "2")]
    assert not raising._around_doomed(
        dels, one_outside, {"1": "c2", "2": "c2"}, "c1")


def test_the_footprint_separates_deletions_from_insertion_points(raising):
    """Точка вставки — точка, а не пустой интервал: `[p, p)` не пересекает
    вообще ничего, и ограда по нему промолчала бы (ревью, круг 3)."""
    parts = [{"kind": "left", "start": 1, "end": 5, "old": "abcd", "new": "x"},
             {"kind": "right", "start": 9, "end": 9, "old": "", "new": "y"}]
    dels, points = raising._write_footprint(parts)
    assert dels == [(1, 5)]
    assert points == [1, 9]


def test_a_named_range_inside_an_unwritten_part_does_not_block(raising):
    """Пометка в нетронутой части операцией физически не задета.

    Отказ по ней был бы глобальным отказом за локальную причину: цитата
    бывает длинной именно ради адресации (STANDARD §9, ревью круг 2).
    """
    op = _op(before="Мы обсудили ", after="фраза подробно")
    _r, (reqs, _s, _p) = _plan(raising, op=op,
                               named=[(2, 6, "named range 'mark'")])
    assert reqs


def test_a_named_range_inside_a_written_part_is_refused(raising):
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, named=[(2, 6, "named range 'mark'")])
    assert exc.value.reason == "named_range_overlap"


def test_an_insertion_exactly_on_a_named_range_border_is_refused(raising):
    """Включит ли Google вставленный на границе текст в сущность — зависит от
    affinity границы и НЕ замерено (M30). Пока не замерено — отказываем."""
    op = _op(before="Мы обсудили ", after="фраза подробно")
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, op=op, named=[(A_E - 1, A_E, "named range 'mark'")])
    assert exc.value.reason == "named_range_overlap"


def test_a_table_of_contents_is_checked_explicitly(raising):
    """Адрес по треду идёт мимо буфера тела, поэтому доказательство
    недостижимости из T4 на этот путь НЕ переносится (предупреждение codex)."""
    tab = _tab()
    tab["body"]["content"].insert(0, {"startIndex": 1, "endIndex": 3,
                                      "tableOfContents": {"content": []}})
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, tab=tab)
    assert exc.value.details["construct"] == "table_of_contents"


def test_a_one_codepoint_anchor_cannot_be_reseated(raising):
    """M29: внутренней позиции у односимвольного якоря нет, вписать носителя
    некуда. Отказ по настоящей причине, а не сырой ошибкой API."""
    para = "а Я б"
    tab = _tab([para])
    snap = _snap(anchors=[(3, 4, "Я", "0")])
    op = _op(before="а ", after="слово б", quote=para)
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, op=op, tab=tab, snap=snap)
    assert exc.value.reason == "comment_anchor_would_be_lost"


def test_a_one_codepoint_anchor_still_gets_an_honest_zero(raising):
    """Ноль распознаётся РАНЬШЕ границы M29: писать нечего, и внутренняя
    позиция для этого не нужна (ревью, круг 2)."""
    para = "а Я б"
    tab = _tab([para])
    snap = _snap(anchors=[(3, 4, "Я", "0")])
    op = _op(before="а ", after="Я б", quote=para)
    _r, (reqs, _s, parts) = _plan(raising, op=op, tab=tab, snap=snap)
    assert reqs is None and parts == []


def test_a_carrierless_edit_says_so_in_human_words(raising):
    """Отказ называет настоящую причину и рабочий выход — без слова «якорь»:
    редактор не запускал скрепку и не обязан знать её устройство."""
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, op=_op(before=" — ", after=" … "))
    assert exc.value.details["construct"] == "no_carrier_word"
    assert "Оставьте рядом хотя бы одно слово" in str(exc.value)
    assert "якор" not in str(exc.value).lower()


# ------------------------------------------------------------ оформление ---

def _styled_tab(bold_range):
    """Абзац, где часть текста жирная: стиль по частям надо на чём-то мерить."""
    tab = _tab()
    el = tab["body"]["content"][0]
    s, e = bold_range
    runs, idx = [], 1
    for text, bold in ((PARA[:s - 1], False), (PARA[s - 1:e - 1], True),
                       (PARA[e - 1:] + "\n", False)):
        if not text:
            continue
        runs.append({"startIndex": idx, "endIndex": idx + len(text),
                     "textRun": {"content": text,
                                 "textStyle": {"bold": True} if bold else {}}})
        idx += len(text)
    el["paragraph"]["elements"] = runs
    return tab


def test_the_carrier_inherits_the_side_it_came_from(raising):
    """Решение владельца: стиль по частям.

    Якорь жирный, правая сторона обычная, носитель взят из `after` — он обязан
    выйти простым. Альтернатива «однородность всего фрагмента, пёстрый —
    отказ» отвергнута: она отрезает обычный случай «прокомментированное слово
    жирное, соседнее обычное». Замерено M30-A2.
    """
    tab = _styled_tab((A_S, A_E))
    _r, (reqs, _s, _p) = _plan(raising, tab=tab)
    assert reqs[3]["updateTextStyle"]["textStyle"] == {}


def test_a_mixed_source_side_is_refused_even_when_it_writes_nothing(raising):
    """Сторона проверяется по ДВУМ основаниям: она поставляет непустую вставку
    ЛИБО поставляет носитель.

    Здесь `after` состоит ровно из слова-носителя, правая вставка пуста — но
    стиль для носителя всё равно читается с правой стороны, и на пёстрой его
    нет (находка ревью, круг 3).
    """
    tab = _styled_tab((A_E + 3, A_E + 6))
    with pytest.raises(raising.PatchOpError):
        _plan(raising, op=_op(after="фраза"), tab=tab)


# --------------------------------------------------------------- квитанция -

def test_the_receipt_states_where_the_conversation_ended_up(raising):
    """Предусловие T10: ответ в тред строится только из ФАКТИЧЕСКОГО эффекта.
    В 0.10 автоответы удалили не за идею, а за то, что они считали эффект по
    устаревшей цитате."""
    tab = _tab()
    snap = _snap()
    r, (_reqs, split, parts) = _plan(raising, tab=tab, snap=snap)
    got = raising._around_effects(tab, r, split, parts, snap["anchors"],
                                  snap["attribution"], [])
    mine = [e for e in got["anchor_effects"] if e["comment_id"] == "c1"]
    assert len(mine) == 1
    assert mine[0]["effect"] == "reseated"
    assert mine[0]["text_before"] == "ЛИШНЕЕ"
    assert mine[0]["text_after"] == "слово"
    assert mine[0]["range_after"] == [O_S + len(split["left"]),
                                      O_S + len(split["left"]) + 5]


def test_a_right_hand_neighbour_is_shifted_by_both_deltas(raising):
    """δ_A + δ_L, а не δ_A: левая часть пишется ПОСЛЕДНЕЙ и двигает всё правее
    себя, ничего там не пересекая.

    Ошибка «только δ_A» оставляет `text_after` верным — поэтому её ловит
    именно диапазон. Замерено M30-D: чужой якорь вернулся ровно там, где ждала
    эта формула.
    """
    para = "Мы обсудили ЛИШНЕЕ слово и другое дело"
    tab = _tab([para])
    snap = _snap(anchors=[(A_S, A_E, "ЛИШНЕЕ", "0"), (20, 27, "слово и", "1")],
                 attribution={"0": "c1", "1": "c2"})
    op = _op(before="Мы кратко обсудили ", after="фраза", quote=para[:24])
    r, (_reqs, split, parts) = _plan(raising, op=op, tab=tab, snap=snap)
    got = raising._around_effects(tab, r, split, parts, snap["anchors"],
                                  snap["attribution"], [])
    foreign = [e for e in got["anchor_effects"] if e["comment_id"] == "c2"]
    assert foreign[0]["text_after"] == " и"
    assert foreign[0]["range_after"] == [25, 27]


def test_closed_threads_keep_their_spoken_caveat(raising):
    """Закрытые треды в выгрузку не попадают вовсе. Промолчать о них — это
    утверждение «не задет», которое запрещено: своё незнание надо называть."""
    tab, snap = _tab(), _snap()
    r, (_reqs, split, parts) = _plan(raising, tab=tab, snap=snap)
    got = raising._around_effects(tab, r, split, parts, snap["anchors"],
                                  snap["attribution"], ["c9"])
    assert got["effects_basis"] == "export-map-open-threads-only"
    assert got["unknown_effect_comment_ids"] == ["c9"]


# ------------------------------------------------------------ через patch --

def _stand(engine, monkeypatch, texts, paras, mutating=True):
    doc = make_doc(texts)
    docs = (MutatingDocsStub if mutating else DocsStub)(doc)
    drive = DriveStub([api_comment("c1", "A", CREATED)],
                      _docx_builder(docs, paras, [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    return docs, drive


def _run(engine, tmp_path, capsys, ops):
    path = tmp_path / "ops.json"
    path.write_text(json.dumps(ops), encoding="utf-8")
    code = 0
    try:
        engine.patch_doc("doc1", str(path))
    except SystemExit as exc:
        code = exc.code
    return code, json.loads(capsys.readouterr().out)


def test_the_word_goes_and_the_conversation_moves_next_door(
        engine, monkeypatch, tmp_path, capsys):
    """Ради чего задача и заведена."""
    docs, _drive = _stand(engine, monkeypatch, [PARA],
                          [(PARA, [("0", *A_OFF)])])
    code, out = _run(engine, tmp_path, capsys, [_op()])
    assert code == 0, json.dumps(out, ensure_ascii=False)[:1500]
    texts = [el["paragraph"]["elements"][0]["textRun"]["content"][:-1]
             for el in docs.base["body"]["content"] if "paragraph" in el]
    assert texts == ["Мы кратко обсудили слово подробно"]


def test_the_batch_never_searches_for_text(engine, monkeypatch, tmp_path,
                                           capsys):
    """Ни одного `replaceAllText`: адрес — это диапазон, а не строка. Иначе
    документ с повторяющимися абзацами снова стал бы неправимым."""
    docs, _drive = _stand(engine, monkeypatch, [PARA],
                          [(PARA, [("0", *A_OFF)])])
    _run(engine, tmp_path, capsys, [_op()])
    batch = semantic_batch(docs)
    assert not any("replaceAllText" in r for r in batch)


def test_the_receipt_reaches_the_caller(engine, monkeypatch, tmp_path,
                                        capsys):
    docs, _drive = _stand(engine, monkeypatch, [PARA],
                          [(PARA, [("0", *A_OFF)])])
    code, out = _run(engine, tmp_path, capsys, [_op()])
    assert code == 0, json.dumps(out, ensure_ascii=False)[:1500]
    note = out["op_notes"][0]
    assert note["applied_as"] == "reseated"
    assert note["text_after"] == "Мы кратко обсудили слово подробно"
    mine = [e for e in note["anchor_effects"] if e["comment_id"] == "c1"]
    assert mine[0]["effect"] == "reseated" and mine[0]["text_after"] == "слово"
    # поздняя операция обязана сказать, что в проверке пересечений не была
    assert "late_bound" in note


# ------------------------------------------------- находки стенда мутаций --
# Четыре мутации пережили первый прогон стенда, и каждая назвала поведение,
# которое никто не проверял. Тесты ниже закрывают ровно их.

def test_a_connector_only_glues_when_a_word_follows_it(engine):
    """Соединитель склеивает, только когда за ним идёт стержень слова.

    Без этого «а-,б» стало бы одним словом, и комментарий переехал бы на кусок
    с запятой внутри. Прежний прогон стенда мутацию «склеивать что угодно»
    пережил: ни один случай её не различал.
    """
    assert engine._lexical_word_spans("а-,б") == [(0, 1), (3, 4)]
    assert engine._lexical_word_spans("из-за") == [(0, 5)]


def test_a_neighbour_in_the_GAP_between_two_deletions_survives(raising):
    """Обречённость считается по каждому удалению, а не по их обхвату.

    Прокомментированное слово остаётся на месте (якорная часть тождественна),
    а обе стороны правятся — между двумя удалениями получается дыра ровно на
    этом слове. Чужой разговор, стоящий в этой дыре, правкой не задет.

    Мутант, считающий покрытие по обхвату «от первого удаления до последнего»,
    отказывает здесь по живому документу. Прежний тест этого не различал:
    в нём чужой якорь лежал вне обхвата и проходил у обоих.
    """
    snap = _snap(anchors=[(A_S, A_E, "ЛИШНЕЕ", "0"),
                          (A_S + 1, A_E - 1, "ИШНЕ", "1")],
                 attribution={"0": "c1", "1": "c2"})
    op = _op(before="Мы кратко обсудили ", after="ЛИШНЕЕ слово кратко")
    _r, (reqs, split, parts) = _plan(raising, op=op, snap=snap)
    assert split["carrier"] == "ЛИШНЕЕ"
    assert [p["kind"] for p in parts] == ["left", "right"]
    assert reqs


def test_an_insertion_point_touching_a_named_range_is_caught(raising):
    """Точка вставки — точка, и ограда обязана мерить её как точку.

    Пометка кончается ровно там, где начнётся вставка: ни одно удаление её не
    задевает, и предикатом по интервалам она не ловится вовсе. Прежний тест
    ловил её удалением, а не точкой, и мутацию предиката переживал.
    """
    tab = _tab(["Раз", PARA])
    base = 1 + len("Раз") + 1
    snap = _snap(anchors=[(base + 12, base + 18, "ЛИШНЕЕ", "0")])
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, tab=tab, snap=snap,
              named=[(base - 3, base, "named range 'mark'")])
    assert exc.value.reason == "named_range_overlap"


def test_an_empty_side_takes_the_anchor_style_not_an_empty_one(raising):
    """У пустой стороны стиля не спросишь, и молчание тут не нейтрально.

    `_range_style` вернул бы пустой словарь, а он уходит с маской по ВСЕМ
    полям и означает «сними оформление», а не «унаследуй»: простое слово
    рядом со ссылкой иначе получает эту ссылку (правка 0.16). Поэтому у
    пустой стороны стиль берётся у якоря.
    """
    para = "ЛИШНЕЕ слово держит абзац"
    tab = _tab([para])
    el = tab["body"]["content"][0]
    el["paragraph"]["elements"] = [
        {"startIndex": 1, "endIndex": 7,
         "textRun": {"content": "ЛИШНЕЕ", "textStyle": {"bold": True}}},
        {"startIndex": 7, "endIndex": 1 + len(para) + 1,
         "textRun": {"content": para[6:] + "\n", "textStyle": {}}}]
    snap = _snap(anchors=[(1, 7, "ЛИШНЕЕ", "0")])
    op = _op(before="Новое ", after="слово держит абзац", quote=para)
    _r, (reqs, _s, parts) = _plan(raising, op=op, tab=tab, snap=snap)
    assert [p["kind"] for p in parts] == ["left", "anchor", "right"]
    left_mask = [q for q in reqs if "updateTextStyle" in q
                 and q["updateTextStyle"]["range"]["startIndex"] == 1][-1]
    assert left_mask["updateTextStyle"]["textStyle"] == {"bold": True}


# ---------------------------------------------------- находки ревью кода ---
# Пять находок первого круга ревью кода. Каждая закрыта тестом, который её
# различает: без него правка была бы «исправлением», которое никто не проверил.

def _mixed_anchor_tab(para, a_off):
    """Абзац, где сам прокомментированный фрагмент пёстрый."""
    tab = _tab([para])
    a_s, a_e = a_off
    mid = (a_s + a_e) // 2
    runs, idx = [], 1
    for text, style in ((para[:a_s], {}), (para[a_s:mid], {"bold": True}),
                        (para[mid:a_e], {}), (para[a_e:] + "\n", {})):
        if not text:
            continue
        runs.append({"startIndex": idx, "endIndex": idx + len(text),
                     "textRun": {"content": text, "textStyle": dict(style)}})
        idx += len(text)
    tab["body"]["content"][0]["paragraph"]["elements"] = runs
    return tab


def test_a_mixed_anchor_cannot_silently_become_one_style(raising):
    """Запасной стиль спрашивается у якоря — значит и он обязан быть один.

    `_range_style` берёт ПЕРВЫЙ пересекающий ран: на пёстром якоре он выдал бы
    стиль его начала за стиль целого, и ссылка или жирность расползлись бы на
    всё новое слово. Видимая порча оформления, которую не поймать ни текстом,
    ни диапазоном.
    """
    para = "Итог зависит от ЛИШНЕГО"
    tab = _mixed_anchor_tab(para, (16, 23))
    snap = _snap(anchors=[(17, 24, "ЛИШНЕГО", "0")])
    # правая сторона нулевой ширины: цитата кончается на самом якоре
    op = _op(before="Итог зависит от ", after="новое", quote=para)
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, op=op, tab=tab, snap=snap)
    assert "оформление" in str(exc.value)


def test_one_foreign_anchor_cut_by_TWO_parts_is_refused(raising):
    """Довод «чужой якорь задет ровно одной частью» держится не всегда.

    Он стоит на ограде внутри перезаписи якоря, а её нет, когда якорная часть
    тождественна и не пишется вовсе: тогда чужой якорь тянется из левого
    удаления через нетронутую середину в правое. Композиция двух частичных
    удалений на ОДНОМ якоре не замерена, а арифметика эффекта выдала бы на
    него две записи с одним `text_before`.
    """
    snap = _snap(anchors=[(A_S, A_E, "ЛИШНЕЕ", "0"),
                          (5, 25, "обсудили ЛИШНЕЕ сло", "1")],
                 attribution={"0": "c1", "1": "c2"})
    op = _op(before="Мы кратко обсудили ", after="ЛИШНЕЕ слово кратко")
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, op=op, snap=snap)
    assert "сразу в двух местах" in str(exc.value)


def test_an_insertion_inside_a_blocked_anchor_is_refused(raising):
    """Огороженный якорь — тот, чью судьбу карта посчитать не берётся.

    Вставка ничего не удаляет, но СТРОГО ВНУТРИ якоря меняет текст под ним
    (M28). Проверять это надо отдельно от удалений: у правки, которая только
    дописывает, удалений нет вовсе, и проверка внутри их цикла не выполнилась
    бы ни разу.
    """
    para = "Итог зависит от ЛИШНЕГО"
    tab = _tab([para])
    snap = _snap(anchors=[(17, 24, "ЛИШНЕГО", "0")],
                 blocked=[(20, 30, "неразличимый якорь")])
    op = _op(before="Итог зависит от ", after="ЛИШНЕГО и дальше", quote=para)
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, op=op, tab=tab, snap=snap)
    assert exc.value.reason == "named_range_overlap"
    assert "неразличимый якорь" in str(exc.value)


def test_an_unsafe_right_word_does_not_silently_move_the_talk_left(engine):
    """Запасное левое слово — только когда справа слова НЕТ.

    Справа слово есть, но отделить его нельзя, не разрезав символ. Молча уехать
    налево значит поставить разговор не с той стороны от правки и не сказать
    об этом.
    """
    split, why = engine._around_carrier_split("хвост длинный", "1️⃣")
    assert split is None and why == "grapheme_cluster"


def test_the_receipt_does_not_claim_a_move_that_did_not_happen(
        engine, monkeypatch, tmp_path, capsys):
    """Слово под комментарием осталось на месте — пересадкой это звать нельзя.

    Иначе два поля одной квитанции противоречат друг другу: `applied_as`
    говорит «переехал», а списка эффектов с пересадкой в ней нет. T10 сообщил
    бы заказчику событие, которого не было.
    """
    docs, _drive = _stand(engine, monkeypatch, [PARA],
                          [(PARA, [("0", *A_OFF)])])
    code, out = _run(engine, tmp_path, capsys, [
        _op(before="Мы кратко обсудили ", after="ЛИШНЕЕ слово кратко")])
    assert code == 0, json.dumps(out, ensure_ascii=False)[:1500]
    note = out["op_notes"][0]
    assert note["applied_as"] == "replace"
    assert "осталось на месте" in note["note"]
    assert not [e for e in note["anchor_effects"]
                if e.get("effect") == "reseated"]
    texts = [el["paragraph"]["elements"][0]["textRun"]["content"][:-1]
             for el in docs.base["body"]["content"] if "paragraph" in el]
    assert texts == ["Мы кратко обсудили ЛИШНЕЕ слово кратко"]


def test_two_pure_insertions_into_one_foreign_anchor_are_refused(raising):
    """Край находки №2, который первая правка не закрыла.

    Удалений нет вовсе: цитата равна прокомментированному слову, носитель ему
    тождествен, обе стороны — чистые вставки. Но обе точки лежат СТРОГО внутри
    чужого якоря, и по M28 обе вставки в него войдут. Считать пересекающие
    удаления тут нечего — считать надо воздействующие части.
    """
    snap = _snap(anchors=[(A_S, A_E, "ЛИШНЕЕ", "0"),
                          (A_S - 1, A_E + 2, "оЛИШНЕЕ с", "1")],
                 attribution={"0": "c1", "1": "c2"})
    op = _op(before="до ", after="ЛИШНЕЕ после", quote="ЛИШНЕЕ")
    with pytest.raises(raising.PatchOpError) as exc:
        _plan(raising, op=op, snap=snap)
    assert "сразу в двух местах" in str(exc.value)
