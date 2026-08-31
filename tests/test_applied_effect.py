"""Контракт фактически применённого эффекта (r19/T7).

Квитанция успешной операции обязана сказать, что правка сделала с текстом под
каждым задетым комментарием. Это предусловие любых автоматических записей в
треды: в 0.10 их удалили (#22) не за идею, а за то, что они срабатывали по
устаревшей цитате `quotedFileContent` и называли текст, которого правка не
касалась. На приёмке это прочиталось как «комментарии отработаны не по смыслу».

Основной вес здесь — таблица замеренных геометрий. Эффект считается
арифметикой по одному правилу (M28): сначала схлопывается удаление, потом
вставка судится по сжавшемуся якорю. Правило объясняет все пятнадцать
геометрий M26 и M28, и каждая из них стоит ниже отдельной строкой — иначе
модель однажды поправят «по смыслу» и заметят это на чужом документе.
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

# Абзац стенда M26/M28: «AAA ЯКОРЬ BBB», якорь на «ЯКОРЬ».
A_S, A_E, A_TEXT = 4, 9, "ЯКОРЬ"
ANCHORS = [(A_S, A_E, A_TEXT, "0")]
ATTR = {"0": "c1"}
NEW = "НОВОЕ"


def _effects(engine, del_start, del_end, new_text=NEW, anchors=None,
             attribution=None, rewritten_cid=None):
    return engine._anchor_effects(
        anchors if anchors is not None else ANCHORS,
        ATTR if attribution is None else attribution,
        del_start=del_start, del_end=del_end, new_text=new_text,
        rewritten_cid=rewritten_cid)


# ---------------------------------------------------------------- замеры ---

BREAK = "НО\nВО"

# (имя замера, удаляемый диапазон, вставляемый текст, эффект, текст якоря,
#  диапазон якоря после правки)
MEASURED = [
    # M28-A. Одиночная вставка: поглощает только строго внутри.
    ("M28-A далеко до",        (2, 2),   NEW,   None,       None, None),
    ("M28-A ровно начало",     (4, 4),   NEW,   None,       None, None),
    ("M28-A строго внутри",    (6, 6),   NEW, "extended", "ЯКНОВОЕОРЬ", [4, 14]),
    ("M28-A ровно конец",      (9, 9),   NEW,   None,       None, None),
    ("M28-A далеко после",     (11, 11), NEW,   None,       None, None),
    # M28-B. Замена внутри якоря, порядок delete → insert.
    ("M28-B обе границы целы", (5, 8),   NEW, "edited", "ЯНОВОЕЬ",   [4, 11]),
    ("M28-B слева впритык",    (4, 7),   NEW, "edited", "РЬ",        [9, 11]),
    ("M28-B справа впритык",   (6, 9),   NEW, "edited", "ЯК",        [4, 6]),
    # M28-C. Новый текст с переводом строки: якорь не рвётся.
    ("M28-C вставка с \\n",    (6, 6),  BREAK, "extended",
     "ЯКНО\nВООРЬ", [4, 14]),
    ("M28-C замена с \\n",     (5, 8),  BREAK, "edited", "ЯНО\nВОЬ", [4, 11]),
    ("M28-C \\n слева впритык", (4, 7), BREAK, "edited", "РЬ",       [9, 11]),
    ("M28-C \\n справа впритык", (6, 9), BREAK, "edited", "ЯК",      [4, 6]),
    # M28-D. Перевод строки НА границе якоря его не задевает.
    ("M28-D \\n на начале",      (4, 4),  BREAK,  None,       None, None),
    ("M28-D \\n на конце",       (9, 9),  BREAK,  None,       None, None),
    ("M28-D замена до якоря",   (1, 4),  BREAK,  None,       None, None),
    ("M28-D замена после",      (9, 12), BREAK,  None,       None, None),
    # M26-A. Чистое удаление, пять геометрий.
    ("M26-A exact",            (4, 9),   "",  "dropped",    None, None),
    ("M26-A covers",           (2, 11),  "",  "dropped",    None, None),
    ("M26-A inside",           (5, 8),   "",  "edited", "ЯЬ",        [4, 6]),
    ("M26-A left",             (2, 6),   "",  "edited", "ОРЬ",       [2, 5]),
    ("M26-A right",            (7, 11),  "",  "edited", "ЯКО",       [4, 7]),
    # M26-B. Пересекающее удаление вместе со вставкой, наш порядок запросов.
    ("M26-B left",             (2, 6),   NEW, "edited", "ОРЬ",       [7, 10]),
    ("M26-B right",            (7, 11),  NEW, "edited", "ЯКО",       [4, 7]),
    ("M26-B covers",           (2, 11),  NEW, "dropped",    None, None),
]


@pytest.mark.parametrize("name,rng,new,effect,text_after,range_after",
                         MEASURED, ids=[m[0] for m in MEASURED])
def test_the_model_reproduces_every_measured_geometry(engine, name, rng, new,
                                                      effect, text_after,
                                                      range_after):
    """Двадцать четыре строки — это двадцать четыре живых документа, на которых поведение
    Google измерено (M26, M28). Расчёт обязан совпадать с каждой.

    `effect is None` означает «правка прошла мимо якоря»: такой якорь в
    отчёте не появляется вовсе. Перечислять нетронутые нельзя — на документе
    с шестьюдесятью тредами это шестьдесят строк шума на каждую правку.

    Диапазон проверяется абсолютными числами, а не длиной. Согласованный
    сдвиг обоих концов сохраняет длину и потому проходит любую сверку
    «диапазон сходится с текстом» — а комментарий при этом объявлен не там,
    где он есть (найдено ревью codex).
    """
    effects, affected = _effects(engine, rng[0], rng[1], new)
    if effect is None:
        assert effects == [] and affected == []
        return
    assert len(effects) == 1
    got = effects[0]
    assert got["effect"] == effect
    assert got["text_after"] == text_after
    assert got["range_after"] == range_after
    assert got["text_before"] == A_TEXT
    assert got["range_before"] == [A_S, A_E]
    assert affected == ["c1"]


@pytest.mark.parametrize("name,rng,new,effect,text_after,range_after",
                         [m for m in MEASURED if m[3] not in (None, "dropped")],
                         ids=[m[0] for m in MEASURED
                              if m[3] not in (None, "dropped")])
def test_the_reported_range_matches_the_reported_text(engine, name, rng, new,
                                                      effect, text_after,
                                                      range_after):
    """Диапазон и текст обязаны сходиться по длине.

    Порознь оба поля выглядят правдоподобно, и именно порознь их и читают.
    Сверка длин ловит расхождение, которого глазами не видно: якорь,
    объявленный на пять единиц длиннее, чем текст, который в него положили.
    """
    effects, _ = _effects(engine, rng[0], rng[1], new)
    r_s, r_e = effects[0]["range_after"]
    assert r_e - r_s == engine._utf16_len(text_after)


def test_a_vanished_anchor_reports_no_range_and_no_text(engine):
    """Накрытый целиком якорь исчез — значит ни диапазона, ни текста у него
    больше нет. Ноль вместо диапазона читался бы как «в начале документа»."""
    effects, _ = _effects(engine, A_S, A_E, "")
    assert effects[0]["range_after"] is None
    assert effects[0]["text_after"] is None


def test_a_rewritten_thread_lands_exactly_on_the_new_text(engine):
    """M26-C: три запроса перезаписи оставляют якорь ровно на вписанном
    тексте, и ни одного исходного символа под ним не остаётся."""
    effects, affected = _effects(engine, A_S, A_E, "СОВСЕМ ИНОЕ",
                                 rewritten_cid="c1")
    assert effects[0]["effect"] == "rewritten"
    assert effects[0]["text_after"] == "СОВСЕМ ИНОЕ"
    assert effects[0]["range_after"] == [A_S, A_S + 11]
    assert affected == ["c1"]


# Перекрывающиеся якоря M28-E: «0» на [4, 9) «ЯКОРЬ», «1» на [6, 12) «ОРЬ BB».
PAIR = [(4, 9, "ЯКОРЬ", "0"), (6, 12, "ОРЬ BB", "1")]
PAIR_ATTR = {"0": "c1", "1": "c2"}


def test_two_overlapping_anchors_are_each_reported_as_measured(engine):
    """M28-E, замерено на живом документе: правка [5, 8) → «НОВОЕ» оставляет
    «0» как «ЯНОВОЕЬ», а «1» как «Ь BB».

    Считать их по отдельности — это утверждение о физике, а не очевидность:
    первый якорь вставку поглощает, второй нет, потому что у второго удаление
    съело всю голову. Пока это не было измерено, квитанция выдавала каждому
    точный текст на одних рассуждениях.
    """
    effects, affected = _effects(engine, 5, 8, NEW, anchors=PAIR,
                                 attribution=PAIR_ATTR)
    assert [(e["comment_id"], e["text_after"]) for e in effects] == [
        ("c1", "ЯНОВОЕЬ"), ("c2", "Ь BB")]
    assert [e["effect"] for e in effects] == ["edited", "edited"]
    assert affected == ["c1", "c2"]


def test_a_covered_anchor_and_its_overlapping_neighbour(engine):
    """M28-E, второй случай: удаление [4, 9) убивает «0» целиком, а «1»
    переживает его и остаётся на « BB». Замерено."""
    effects, affected = _effects(engine, 4, 9, "", anchors=PAIR,
                                 attribution=PAIR_ATTR)
    assert [(e["comment_id"], e["effect"], e["text_after"]) for e in effects
            ] == [("c1", "dropped", None), ("c2", "edited", " BB")]
    assert affected == ["c1", "c2"]


TRIO = [(2, 7, "A ЯКО", "0"), (4, 9, "ЯКОРЬ", "1"), (6, 12, "ОРЬ BB", "2")]


def test_three_overlapping_anchors_get_three_different_verdicts(engine):
    """M28-F, замерено: одна правка [5, 8) → «НОВОЕ» на трёх перекрывающихся
    якорях даёт три РАЗНЫХ исхода.

    Первому вставка не достаётся — у него удаление съело хвост. Второй её
    поглощает. Третьему тоже не достаётся — у него удаление съело голову. Пара
    из блока E независимость показала, но пара это пара; здесь правило
    работает там, где ошибиться было бы легче всего.
    """
    effects, affected = _effects(
        engine, 5, 8, NEW, anchors=TRIO,
        attribution={"0": "c1", "1": "c2", "2": "c3"})
    assert [(e["text_after"], e["range_after"]) for e in effects] == [
        ("A Я", [2, 5]), ("ЯНОВОЕЬ", [4, 11]), ("Ь BB", [10, 14])]
    assert affected == ["c1", "c2", "c3"]


def test_only_the_rewritten_thread_gets_the_rewritten_verdict(engine):
    """`rewritten_cid` — это адрес одного треда, а не режим отчёта. Соседний
    якорь в том же диапазоне считается обычной арифметикой."""
    anchors = ANCHORS + [(A_S, A_E + 2, "ЯКОРЬ B", "1")]
    effects, _ = _effects(engine, A_S, A_E, "ИНОЕ", anchors=anchors,
                          attribution={"0": "c1", "1": "c2"},
                          rewritten_cid="c1")
    verdicts = {e["comment_id"]: e["effect"] for e in effects}
    assert verdicts == {"c1": "rewritten", "c2": "edited"}


def test_an_anchor_nobody_can_name_is_reported_but_not_counted(engine):
    """Пролёт без атрибуции — это задетый якорь, который не с чем связать.
    Молчать о нём нельзя, но и в список тредов ему нельзя: там id, по
    которым потом пишут ответы."""
    effects, affected = _effects(engine, 5, 8, NEW, attribution={})
    assert len(effects) == 1 and effects[0]["comment_id"] is None
    assert affected == []


def test_one_thread_is_counted_once_however_many_anchors_it_has(engine):
    anchors = [(A_S, A_E, A_TEXT, "0"), (A_S + 1, A_E, "КОРЬ", "0")]
    _effects_, affected = _effects(engine, 5, 8, NEW, anchors=anchors)
    assert affected == ["c1"]
    assert len(_effects_) == 2


# ------------------------------------------------------------- UTF-16 -----

def test_an_emoji_is_two_units_and_the_text_is_cut_by_units(engine):
    """Смещения Docs считаются в единицах UTF-16, а срез питоновской строки —
    в code points. На тексте с эмодзи они расходятся, и расходятся тихо."""
    anchors = [(0, 4, "a\U0001F600b", "0")]
    effects, _ = _effects(engine, 1, 3, "", anchors=anchors)
    assert effects[0]["text_after"] == "ab"


def test_a_cut_inside_a_surrogate_pair_names_no_text(engine):
    """Разрез пришёлся внутрь эмодзи: дословного текста не существует.
    Приблизительный назвать хуже, чем никакого — на приблизительном и
    построили автоответы, удалённые в 0.10."""
    anchors = [(0, 4, "a\U0001F600b", "0")]
    effects, _ = _effects(engine, 1, 2, "", anchors=anchors)
    assert effects[0]["text_after"] is None
    assert effects[0]["effect"] == "edited"       # геометрия известна и верна


def test_an_anchor_whose_text_and_geometry_disagree_names_no_text(engine):
    """Внутри якоря сидит инлайновый объект: длина текста и длина диапазона
    расходятся, и любая арифметика по смещениям адресует не туда."""
    anchors = [(0, 10, "abc", "0")]
    effects, _ = _effects(engine, 1, 3, "X", anchors=anchors)
    assert effects[0]["text_after"] is None


def test_an_inverted_range_is_refused_before_the_write(engine):
    """Перевёрнутый диапазон не приходит от разрешения операции ни одним
    путём — но если однажды придёт, отказать надо ДО записи.

    Пропустить его дальше значит записать что-то одно, а в квитанции
    построить на этих координатах правдоподобный текст, которого в документе
    нет: `_anchor_effects` посчитал бы «удаление» отрицательной длины как
    вставку и объявил бы точный эффект. Найдено ревью codex.
    """
    with pytest.raises(engine.PatchOpError) as exc:
        engine._execute_index_replace(None, "doc1", None, 12, 4, "X", None,
                                      "R0")
    assert exc.value.state == "not_applied"
    assert "inverted range" in str(exc.value)


def test_utf16_cut_refuses_what_it_cannot_do(engine):
    assert engine._utf16_cut("abc", 1) == ("a", "bc")
    assert engine._utf16_cut("abc", 3) == ("abc", "")
    assert engine._utf16_cut("abc", 4) is None       # за концом строки
    assert engine._utf16_cut("abc", -1) is None
    assert engine._utf16_cut("\U0001F600", 1) is None  # внутри пары


def test_every_declared_effect_is_one_the_contract_knows(engine):
    """Список эффектов закрыт, и в нём нет ни одного значения, которого
    никто не производит: непроизводимое значение — это обещание, а не
    контракт (ту же ошибку убирали в T4 из недостижимой ограды)."""
    produced = {e[3] for e in MEASURED if e[3]} | {"rewritten"}
    assert produced == engine._ANCHOR_EFFECTS


# --------------------------------------------------------- через patch ----

TEXTS = ["Alpha", "Bravo", "Charlie"]


def _stand(engine, monkeypatch, paras=None, comments=None):
    docs = DocsStub(make_doc(TEXTS))
    drive = DriveStub(
        [api_comment(c, "A", CREATED) for c in (comments or ["c1"])],
        _docx_builder(docs,
                      paras or [("Alpha", []), ("Bravo", [("0", 0, 5)]),
                                ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    return docs, drive


def _run(engine, tmp_path, ops, capsys):
    path = tmp_path / "ops.json"
    path.write_text(json.dumps(ops), encoding="utf-8")
    code = 0
    try:
        engine.patch_doc("doc1", str(path))
    except SystemExit as exc:
        code = exc.code
    return code, json.loads(capsys.readouterr().out)


def test_a_plain_replace_reports_what_it_did_to_the_thread(engine, monkeypatch,
                                                           tmp_path, capsys):
    """«rav» → «ROCK» внутри прокомментированного «Bravo»: часть исходного
    текста ушла, часть цела, вставка поглощена якорем (M28-B)."""
    _docs, _drive = _stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "rav", "with": "ROCK"}], capsys)
    assert code == 0
    note = out["op_notes"][0]
    assert note["applied_as"] == "replace"
    assert note["effects_basis"] == "export-map"
    assert note["text_before"] == "rav" and note["text_after"] == "ROCK"
    # «rav» стоит на индексах [8, 11) второго абзаца, «ROCK» на единицу длиннее
    assert note["range_before"] == [8, 11]
    assert note["range_after"] == [8, 12]
    assert note["affected_comment_ids"] == ["c1"]
    eff = note["anchor_effects"][0]
    assert eff["effect"] == "edited"
    assert eff["text_before"] == "Bravo" and eff["text_after"] == "BROCKo"
    # якорь стоял на [7, 12), новый текст на единицу длиннее старого
    assert eff["range_before"] == [7, 12] and eff["range_after"] == [7, 13]


def test_a_narrowed_edit_reports_the_range_it_actually_wrote(engine,
                                                             monkeypatch,
                                                             tmp_path,
                                                             capsys):
    """«Bravo» → «Bruvo» сужается до одной буквы. Квитанция обязана назвать
    сужённый диапазон: широкий заказанный — это ровно тот устаревший адрес,
    из-за которого автоответы говорили неправду."""
    _docs, _drive = _stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "Bravo", "with": "Bruvo"}], capsys)
    assert code == 0
    note = out["op_notes"][0]
    assert note["applied_as"] == "narrowed"
    assert note["text_before"] == "a" and note["text_after"] == "u"
    assert note["range_before"] == [9, 10]        # не [7, 12]
    eff = note["anchor_effects"][0]
    assert eff["text_before"] == "Bravo" and eff["text_after"] == "Bruvo"
    # длина не изменилась, значит и якорь остался на своём месте целиком
    assert eff["range_after"] == [7, 12]


def test_a_rewrite_says_the_thread_now_sits_on_new_text(engine, monkeypatch,
                                                        tmp_path, capsys):
    """Перезапись накрытого якоря: под комментарием не осталось ни одного
    символа из того, что человек выделял, и это ровно то, о чём живой автор
    говорит в треде."""
    _docs, _drive = _stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}], capsys)
    assert code == 0
    note = out["op_notes"][0]
    assert note["applied_as"] == "rewritten"
    eff = note["anchor_effects"][0]
    assert eff["comment_id"] == "c1" and eff["effect"] == "rewritten"
    assert eff["text_before"] == "Bravo" and eff["text_after"] == "Zulu"
    assert eff["range_after"] == [7, 11]


REPLY_SEC = "2026-07-13T17:55:10Z"


def test_an_anchor_of_a_surviving_thread_can_still_disappear(engine,
                                                             monkeypatch,
                                                             tmp_path, capsys):
    """Один разговор держится на двух якорях — в выгрузке это две записи с
    разными docx id, сведённые к одному треду по свидетелю (автор, секунда).

    Накрыть один из якорей можно: тред жив на втором, и правка проходит. Но
    исчезнувший якорь обязан быть назван — человек увидит свой комментарий
    там, где текста, на котором он его писал, уже нет.
    """
    from test_sync_anchors import _reply

    docs = DocsStub(make_doc(TEXTS))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED,
                     replies=[_reply("r1", "B", REPLY_SEC)])],
        _docx_builder(docs, [("Alpha", [("0", 0, 5)]),
                             ("Bravo", [("1", 0, 5)]), ("Charlie", [])],
                      [("0", "A", CREATED_SEC), ("1", "B", REPLY_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    code, out = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}], capsys)
    assert code == 0, json.dumps(out, ensure_ascii=False)[:1200]
    effs = out["op_notes"][0]["anchor_effects"]
    assert [e["effect"] for e in effs] == ["dropped"]
    assert effs[0]["comment_id"] == "c1"
    assert effs[0]["text_before"] == "Bravo"
    assert effs[0]["range_after"] is None
    # и второй якорь того же треда правка не касалась — в отчёте его нет
    assert out["op_notes"][0]["affected_comment_ids"] == ["c1"]


def test_a_closed_thread_makes_the_map_say_what_it_does_not_cover(
        engine, monkeypatch, tmp_path, capsys):
    """Закрытый тред в выгрузку не попадает вовсе (M13): ни записи в
    `comments.xml`, ни разметки в тексте. Значит карта знает только открытые
    треды — а правка могла пройти ровно по якорю закрытого.

    Промолчать об этом нельзя: пустой список эффектов прочитался бы как «этот
    комментарий не задет». Найдено ревью codex.
    """
    docs = DocsStub(make_doc(TEXTS))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED),
         api_comment("c2", "A", "2026-07-13T18:10:00.000Z", resolved=True)],
        _docx_builder(docs, [("Alpha", []), ("Bravo", [("0", 0, 5)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    code, out = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "rav", "with": "ROCK"}], capsys)
    assert code == 0, json.dumps(out, ensure_ascii=False)[:1200]
    note = out["op_notes"][0]
    assert note["effects_basis"] == "export-map-open-threads-only"
    assert note["unknown_effect_comment_ids"] == ["c2"]
    # и то, что карта ЗНАЕТ, она докладывает как обычно
    assert note["affected_comment_ids"] == ["c1"]


def test_without_closed_threads_the_map_is_complete(engine, monkeypatch,
                                                    tmp_path, capsys):
    """Зеркало предыдущего: без закрытых тредов карта покрывает документ
    целиком, и оговорке в квитанции взяться неоткуда. Без этой половины
    предыдущий тест не отличал бы честную оговорку от постоянной."""
    _docs, _drive = _stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "rav", "with": "ROCK"}], capsys)
    assert code == 0
    note = out["op_notes"][0]
    assert note["effects_basis"] == "export-map"
    assert "unknown_effect_comment_ids" not in note


def test_an_insert_says_plainly_that_it_did_not_look(engine, monkeypatch,
                                                     tmp_path, capsys):
    """Вставка ничего не удаляет и потому экспортную карту не строит (C5).
    Пустой список эффектов здесь был бы враньём: «никого не задели» и «мы не
    смотрели» — разные утверждения, и второе честнее."""
    _docs, _drive = _stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, [
        {"op": "insert_after_quote", "quote": "Alpha", "text": " ещё"}],
        capsys)
    assert code == 0
    note = out["op_notes"][0]
    assert note["applied_as"] == "insert"
    assert note["effects_basis"] == "not-mapped"
    assert "anchor_effects" not in note
    assert "affected_comment_ids" not in note


def test_an_extending_replace_is_an_insert_and_says_so(engine, monkeypatch,
                                                       tmp_path, capsys):
    _docs, _drive = _stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "Charlie", "with": "Charlie plus"}],
        capsys)
    assert code == 0
    note = out["op_notes"][0]
    assert note["applied_as"] == "insert"
    assert note["text_after"] == " plus"
    assert note["effects_basis"] == "not-mapped"


def test_a_no_op_declares_no_effect_at_all(engine, monkeypatch, tmp_path,
                                           capsys):
    """Ничего не записано — значит объявлять нечего. Пустой эффект тут был бы
    поводом ответить в тред о правке, которой не было."""
    _docs, _drive = _stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "Bravo", "with": "Bravo"}], capsys)
    assert code == 0
    note = out["op_notes"][0]
    assert note["applied_as"] == "no-op"
    for field in ("range_after", "text_after", "anchor_effects",
                  "affected_comment_ids", "effects_basis"):
        assert field not in note


def test_a_refusal_declares_no_effect_at_all(engine, monkeypatch, tmp_path,
                                             capsys):
    """Отказ несёт причину и выход, но не эффект: применено ничего не было."""
    _docs, _drive = _stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "Nothing", "with": "X"}], capsys)
    assert code == 3
    entry = out["refused"][0]
    assert entry["reason"] == "quote_not_found"
    for field in ("applied_as", "anchor_effects", "affected_comment_ids"):
        assert field not in entry


def test_a_document_without_comments_says_there_was_nobody_to_touch(
        engine, monkeypatch, tmp_path, capsys):
    """Отдельный путь записи — отдельный повод промолчать. Пустой список
    здесь стоит явно, чтобы вызывающему не приходилось выводить его из
    названия стратегии."""
    docs = DocsStub(make_doc(TEXTS))
    drive = DriveStub([], _docx_builder(docs, [(t, []) for t in TEXTS], []))
    wire(engine, monkeypatch, docs, drive)
    code, out = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}], capsys)
    assert code == 0
    assert out["strategy"] == "index-atomic"
    assert out["affected_comment_ids"] == []


def test_every_applied_operation_gets_exactly_one_receipt(engine, monkeypatch,
                                                          tmp_path, capsys):
    """Единый результат — значит на каждую применённую операцию ровно одна
    запись. Две читались бы как две операции, ноль — как «эта прошла молча»,
    и в обоих случаях автоответ уходит не туда."""
    _docs, _drive = _stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, [
        {"op": "replace_quote", "quote": "Alpha", "with": "Omega"},
        {"op": "replace_quote", "quote": "rav", "with": "ROCK"},
    ], capsys)
    assert code == 0
    assert out["ops_applied"] == 2
    assert len(out["op_notes"]) == 2
    assert [n["applied_as"] for n in out["op_notes"]] == ["replace", "replace"]
