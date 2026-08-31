"""Правка адресуется тредом, а не текстом (#52, r19/T8).

Пост-мортем 19 августа: якорь короче слова на прокомментированном документе
адресовать нечем, если его текст неуникален. Цитата отказывает по
неоднозначности, номер вхождения не спасает, когда повторяется и окружение —
а для рассылок и курсов повторяющийся текст это норма формата, а не небрежность.

`replace_anchor` — это АДРЕСАЦИЯ, а не новый писатель: диапазон берётся из
свежей карты выгрузки и уходит в тот же путь записи со всеми его оградами.
Отсюда и главный риск задачи: адрес по треду отменяет доказательства
недостижимости, на которых стояли ограды адреса по цитате.
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
    _reply,
    api_comment,
    make_doc,
    semantic_batch,
    wire,
    written_text,
)

REPLY_SEC = "2026-07-13T17:55:10Z"


def _run(engine, tmp_path, capsys, ops):
    path = tmp_path / "ops.json"
    path.write_text(json.dumps(ops), encoding="utf-8")
    code = 0
    try:
        engine.patch_doc("doc1", str(path))
    except SystemExit as exc:
        code = exc.code
    return code, json.loads(capsys.readouterr().out)


def _stand(engine, monkeypatch, texts, paras, comments=None,
           api=None, named_ranges=None, mutating=False):
    doc = make_doc(texts, named_ranges=named_ranges)
    docs = (MutatingDocsStub if mutating else DocsStub)(doc)
    drive = DriveStub(
        api if api is not None else [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, paras,
                      comments if comments is not None
                      else [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    return docs, drive


# --------------------------------------------------------------- главное ---

TWINS = ["Alpha", "Копия", "Charlie", "Копия"]


def _twin_stand(engine, monkeypatch, on_second=True, mutating=False):
    """Два дословно одинаковых абзаца, комментарий на одном из них.

    Живая форма #52: цитатой такой якорь не адресуется вовсе, и номер
    вхождения тут не помощник — вокруг тоже всё повторяется.
    """
    paras = [("Alpha", []),
             ("Копия", [] if on_second else [("0", 0, 5)]),
             ("Charlie", []),
             ("Копия", [("0", 0, 5)] if on_second else [])]
    return _stand(engine, monkeypatch, TWINS, paras, mutating=mutating)


def test_the_edit_lands_on_the_commented_copy_and_not_the_other(
        engine, monkeypatch, tmp_path, capsys):
    """Ради этого задача и заведена: из двух одинаковых абзацев правится тот,
    на котором висит разговор."""
    docs, _drive = _twin_stand(engine, monkeypatch, mutating=True)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"}])
    assert code == 0, json.dumps(out, ensure_ascii=False)[:1200]
    texts = [el["paragraph"]["elements"][0]["textRun"]["content"][:-1]
             for el in docs.base["body"]["content"] if "paragraph" in el]
    assert texts == ["Alpha", "Копия", "Charlie", "Правка"]


def test_the_batch_addresses_by_index_and_never_searches(engine, monkeypatch):
    """Поиска по тексту в батче нет вовсе — иначе правка нашла бы обе копии,
    и вся задача была бы бессмысленной."""
    docs, drive = _twin_stand(engine, monkeypatch)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    engine._apply_op_anchor_safe(docs, drive, "doc1", {
        "op": "replace_anchor", "comment_id": "c1", "with": "Правка"}, None)
    main = semantic_batch(docs)
    assert not any("replaceAllText" in r for r in main)
    assert written_text(main) == "Правка"


def test_the_receipt_says_the_thread_now_sits_on_the_new_text(
        engine, monkeypatch, tmp_path, capsys):
    """Контракт эффекта T7 на новой операции: под комментарием не осталось
    ни одного символа из того, что человек выделял, и квитанция это говорит."""
    _docs, _drive = _twin_stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"}])
    assert code == 0
    note = out["op_notes"][0]
    assert note["source"] == "comment='c1'"
    assert note["applied_as"] == "rewritten"
    assert note["affected_comment_ids"] == ["c1"]
    eff = note["anchor_effects"][0]
    assert eff["effect"] == "rewritten"
    assert eff["text_before"] == "Копия" and eff["text_after"] == "Правка"
    # и вслух сказано, что в проверке пересечений она не участвовала
    assert "в проверке пересечений" in note["late_bound"]


# ------------------------------------------- сужение против семантики ------

def test_a_trailing_change_rewrites_instead_of_cutting_at_the_border(
        engine, monkeypatch, tmp_path, capsys):
    """`Копия` → `Копил`: общий аффикс есть только слева, значит внутреннего
    среза не существует.

    Граничный срез документ бы поправил, но вставка на границе якорем не
    поглощается (M28), и разговор остался бы на `Копи` — операция сделала бы
    не то, что просили, и промолчала. Поэтому здесь работает перезапись.
    """
    _docs, _drive = _twin_stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Копил"}])
    assert code == 0
    note = out["op_notes"][0]
    assert note["applied_as"] == "rewritten"
    assert note["anchor_effects"][0]["text_after"] == "Копил"


def test_an_inner_change_is_narrowed_to_what_changed(engine, monkeypatch,
                                                     tmp_path, capsys):
    """`Копия` → `Копея`: аффиксы с обеих сторон, внутренний срез есть.

    Правка ужимается до одной буквы, и комментарий всё равно покрывает весь
    новый текст: обе стороны пережили удаление, значит вставка пришлась
    строго внутрь остатка и была поглощена (M28-B).
    """
    _docs, _drive = _twin_stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Копея"}])
    assert code == 0
    note = out["op_notes"][0]
    assert note["applied_as"] == "narrowed"
    assert note["text_before"] == "и" and note["text_after"] == "е"
    assert note["anchor_effects"][0]["text_after"] == "Копея"


def test_extending_the_text_keeps_the_whole_of_it_under_the_comment(
        engine, monkeypatch, tmp_path, capsys):
    """«Допиши фразу» для правки по треду значит «под комментарием теперь
    весь новый текст».

    Обычная замена выполнила бы это голой вставкой — быстро, безопасно и не
    то: вставка на границе якорем не поглощается, дописанное оказалось бы
    снаружи разговора. Здесь короткий путь сознательно не берётся.
    """
    _docs, _drive = _twin_stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Копия и хвост"}])
    assert code == 0
    note = out["op_notes"][0]
    assert note["applied_as"] == "rewritten"
    assert note["anchor_effects"][0]["text_after"] == "Копия и хвост"


# ------------------------------------------------------ адрес не сходится --

def test_an_unknown_thread_is_refused_before_any_write(engine, monkeypatch,
                                                       tmp_path, capsys):
    docs, _drive = _twin_stand(engine, monkeypatch)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "нет-такого", "with": "X"}])
    assert code == 3
    entry = out["refused"][0]
    assert entry["reason"] == "comment_thread_unresolvable"
    # отказ обязан называть, КАКАЯ из правок отклонена: «которая из моих
    # десяти» — первое, что спрашивают у отказа, а разрешённой записи, из
    # которой берётся имя, у поздней операции нет
    assert entry["source"] == "comment='нет-такого'"
    assert docs.canary_text is None       # служебная строка убрана
    assert not [b for b in docs.batches
                if any("deleteContentRange" in r for r in b[1:])]


def test_a_closed_thread_is_refused(engine, monkeypatch, tmp_path, capsys):
    """Закрытый тред в выгрузку не попадает вовсе (M13), значит якоря у него
    нет и адресовать по нему нечего. Отказ, а не молчаливый промах."""
    docs, _drive = _stand(
        engine, monkeypatch, TWINS,
        [("Alpha", []), ("Копия", []), ("Charlie", []), ("Копия", [])],
        comments=[],
        api=[api_comment("c1", "A", CREATED, resolved=True)])
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "X"}])
    assert code == 3
    assert out["refused"][0]["reason"] == "comment_thread_unresolvable"
    assert docs.canary_text is None


def test_a_thread_with_two_anchors_is_refused_not_guessed(engine, monkeypatch,
                                                          tmp_path, capsys):
    """Один разговор на двух местах: какое из них человек имел в виду, знает
    только он. Выбирать копию за него скрепка не станет."""
    docs, _drive = _stand(
        engine, monkeypatch, TWINS,
        [("Alpha", [("0", 0, 5)]), ("Копия", []), ("Charlie", []),
         ("Копия", [("1", 0, 5)])],
        comments=[("0", "A", CREATED_SEC), ("1", "B", REPLY_SEC)],
        api=[api_comment("c1", "A", CREATED,
                         replies=[_reply("r1", "B", REPLY_SEC)])])
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "X"}])
    assert code == 3
    entry = out["refused"][0]
    assert entry["reason"] == "comment_thread_has_multiple_anchors"
    assert entry["details"]["anchors"] == 2
    assert docs.canary_text is None


def test_reply_records_do_not_count_as_a_second_anchor(engine, monkeypatch,
                                                       tmp_path, capsys):
    """Ответ несёт в выгрузке диапазон родителя, то есть физически то же
    место. Считать его вторым якорем значило бы запретить правку по треду
    всякому разговору, в котором кто-то ответил."""
    _docs, _drive = _stand(
        engine, monkeypatch, TWINS,
        [("Alpha", []), ("Копия", []), ("Charlie", []),
         ("Копия", [("0", 0, 5), ("1", 0, 5)])],
        comments=[("0", "A", CREATED_SEC), ("1", "B", REPLY_SEC)],
        api=[api_comment("c1", "A", CREATED,
                         replies=[_reply("r1", "B", REPLY_SEC)])])
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"}])
    assert code == 0, json.dumps(out, ensure_ascii=False)[:900]
    assert out["op_notes"][0]["applied_as"] == "rewritten"


# --------------------------------------------------- ограды нового адреса --

SINGLE = ["Alpha", "Копия", "Charlie"]


def _single(engine, monkeypatch, anchor=(0, 5), named_ranges=None,
            mutating=False, texts=None):
    return _stand(engine, monkeypatch, texts or SINGLE,
                  [("Alpha", []), ("Копия", [("0",) + anchor]),
                   ("Charlie", [])],
                  named_ranges=named_ranges, mutating=mutating)


def test_a_named_range_over_the_anchor_stops_the_edit(engine, monkeypatch,
                                                      tmp_path, capsys):
    """Доказательство недостижимости, на котором стоял адрес по цитате, этим
    адресом отменяется: координаты приходят прямо из карты.

    Правка по треду никогда не адресует именованный диапазон по имени, значит
    любое пересечение с ним побочное — и резать чужую машинную пометку ради
    неё не за что. Найдено ревью codex.
    """
    docs, _drive = _single(engine, monkeypatch, named_ranges={
        "mark1": {"namedRanges": [{"ranges": [
            {"startIndex": 8, "endIndex": 11}]}]}})
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"}])
    assert code == 3
    entry = out["refused"][0]
    assert entry["reason"] == "named_range_overlap"
    assert "mark1" in entry["error"]
    assert docs.canary_text is None


def test_a_table_of_contents_over_the_anchor_stops_the_edit(engine,
                                                            monkeypatch,
                                                            tmp_path, capsys):
    """Та же причина: адрес по треду отменяет доказательство, что в
    оглавление попасть нечем."""
    docs, _drive = _single(engine, monkeypatch)
    # вставкой, а не в конец: оглавление должно ПЕРЕСЕКАТЬ якорь, а
    # последний элемент тела задаёт длину документа
    docs.base["body"]["content"].insert(
        1, {"startIndex": 7, "endIndex": 12, "tableOfContents": {}})
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"}])
    assert code == 3
    entry = out["refused"][0]
    assert entry["reason"] == "unsupported_structure"
    assert "оглавлени" in entry["error"]
    for forbidden in ("в интерфейсе", "руками", "в UI"):
        assert forbidden not in entry["error"]
    assert docs.canary_text is None


def test_a_suggestion_over_the_anchor_stops_the_edit(engine, monkeypatch,
                                                     tmp_path, capsys):
    docs, _drive = _single(engine, monkeypatch)
    para = docs.base["body"]["content"][1]
    para["paragraph"]["elements"][0]["textRun"]["suggestedInsertionIds"] = \
        ["sug1"]
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"}])
    assert code == 3
    assert out["refused"][0]["reason"] == "suggestion_overlap"
    assert docs.canary_text is None


def test_a_one_character_anchor_is_refused_with_editorial_advice(
        engine, monkeypatch, tmp_path, capsys):
    """Замерено (M29): однобуквенный якорь переписать нечем ни одним
    способом — обе формы обхода уносят разговор.

    Совет обязан быть редакторским: выделить комментарием чуть больше
    текста. Отправлять человека править в интерфейсе тут значило бы
    предлагать ему сделать за нас техническую работу.
    """
    docs, _drive = _single(engine, monkeypatch, anchor=(0, 1))
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"}])
    assert code == 3
    err = out["refused"][0]["error"]
    assert out["refused"][0]["reason"] == "comment_anchor_would_be_lost"
    assert "чуть больше текста" in err
    assert "в интерфейсе" not in err
    assert docs.canary_text is None


# ------------------------------------------------------- схема и дубли -----

@pytest.mark.parametrize("op,fragment", [
    ({"op": "replace_anchor", "with": "X"}, "comment_id"),
    ({"op": "replace_anchor", "comment_id": "", "with": "X"}, "comment_id"),
    ({"op": "replace_anchor", "comment_id": 17, "with": "X"}, "comment_id"),
    ({"op": "replace_anchor", "comment_id": "c1"}, "'with'"),
    ({"op": "replace_anchor", "comment_id": "c1", "with": ""}, "'with'"),
    ({"op": "replace_anchor", "comment_id": "c1", "with": "два\nабзаца"},
     "перевод"),
])
def test_a_broken_op_is_named_a_broken_op_and_not_a_late_target(
        engine, monkeypatch, tmp_path, capsys, op, fragment):
    """Ошибка схемы обязана выглядеть ошибкой схемы. У этой операции цель по
    построению разрешается поздно, и если валить одно в другое, настоящие
    опечатки в `comment_id` потеряются среди нормального хода вещей."""
    docs, _drive = _single(engine, monkeypatch)
    code, out = _run(engine, tmp_path, capsys, [op])
    assert code == 3
    assert fragment in out["refused"][0]["error"]
    assert docs.canary_text is None
    assert docs.batches == []          # до карты дело не дошло вовсе


def test_two_edits_on_one_thread_refuse_both(engine, monkeypatch, tmp_path,
                                             capsys):
    """Какую из двух применить, знает только человек. Отклоняются обе — так
    же, как пересекающиеся диапазоны."""
    _docs, _drive = _single(engine, monkeypatch)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Первая"},
        {"op": "replace_anchor", "comment_id": "c1", "with": "Вторая"},
    ])
    assert code == 3
    assert len(out["refused"]) == 2
    assert out["ops_applied"] == 0
    assert all("одному треду" in e["error"] for e in out["refused"])


def test_a_document_without_comments_has_nothing_to_address(engine,
                                                            monkeypatch,
                                                            tmp_path, capsys):
    docs = DocsStub(make_doc(SINGLE))
    drive = DriveStub([], _docx_builder(docs, [(t, []) for t in SINGLE], []))
    wire(engine, monkeypatch, docs, drive)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"}])
    assert code == 3
    entry = out["refused"][0]
    assert entry["reason"] == "comment_thread_unresolvable"
    assert "нет заякоренных комментариев" in entry["error"]
    assert docs.batches == []


# ------------------------------------------ последовательность в файле -----

def test_a_thread_already_touched_by_this_run_is_refused(engine, monkeypatch,
                                                         tmp_path, capsys):
    """Адрес этой операции — тред, и любой текст под ним для неё законен.

    Значит после соседней правки, сдвинувшей разговор, она молча накрыла бы
    её результат целиком, и обе числились бы применёнными. У обычной замены
    этой беды нет: она сверяет текст в разрешённом диапазоне с тем, по
    которому адресовалась. Найдено ревью codex.
    """
    _docs, _drive = _single(engine, monkeypatch, mutating=True)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_quote", "quote": "опи", "with": "ОПИ"},
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"},
    ])
    assert code == 3
    assert out["ops_applied"] == 1
    entry = out["refused"][0]
    assert entry["reason"] == "concurrent_edit"
    assert "уже задет правкой" in entry["error"]


def test_an_ordinal_that_moved_under_the_run_is_refused(engine, monkeypatch,
                                                        tmp_path, capsys):
    """Номер вхождения осмыслен только вместе с числом копий.

    Три одинаковых абзаца, разговор на первом. Правка по треду убирает одну
    копию — и `occurrence: 2` теперь показывает на бывшую третью. Проверка
    «в этом диапазоне та самая цитата» на близнецах тавтологична и
    промолчала бы, а правка уехала бы в чужую копию. Найдено ревью codex.
    """
    texts = ["Копия", "Копия", "Копия"]
    _docs, _drive = _stand(
        engine, monkeypatch, texts,
        [("Копия", [("0", 0, 5)]), ("Копия", []), ("Копия", [])],
        mutating=True)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Первая"},
        {"op": "replace_quote", "quote": "Копия", "occurrence": 2,
         "with": "Вторая"},
    ])
    assert code == 3
    assert out["ops_applied"] == 1
    entry = out["refused"][0]
    assert entry["reason"] == "concurrent_edit"
    assert entry["details"] == {"expected_matches": 3, "found_matches": 2}


def test_an_op_without_an_ordinal_still_rides_on_its_uniqueness(
        engine, monkeypatch, tmp_path, capsys):
    """Зеркало предыдущего, без которого оно доказывало бы лишнее.

    Операция БЕЗ номера вхождения защищена уникальностью: одно совпадение и
    есть доказательство цели, сколько бы их ни было раньше. Правка, ставшая
    однозначной благодаря соседке, обязана по-прежнему проходить (#36).
    """
    texts = ["Копия", "Копия", "Charlie"]
    _docs, _drive = _stand(
        engine, monkeypatch, texts,
        [("Копия", [("0", 0, 5)]), ("Копия", []), ("Charlie", [])],
        mutating=True)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Первая"},
        {"op": "replace_quote", "quote": "Копия", "with": "Вторая"},
    ])
    assert code == 0, json.dumps(out, ensure_ascii=False)[:900]
    assert out["ops_applied"] == 2


# ------------------------------------------------- владение канарейкой -----

def test_an_unexpected_failure_after_the_canary_still_removes_it(
        engine, monkeypatch):
    """Поздний адрес приводит под канарейку целый резолвер. Оставить его
    отказы и его аварии на дисциплину вызывающего значит однажды забыть
    служебную строку в чужом документе. Найдено ревью codex."""
    docs, drive = _single(engine, monkeypatch)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)

    def boom(*a, **kw):
        raise RuntimeError("резолвер упал")

    monkeypatch.setattr(engine, "_resolve_anchor_target", boom)
    with pytest.raises(RuntimeError):
        engine._apply_op_anchor_safe(docs, drive, "doc1", {
            "op": "replace_anchor", "comment_id": "c1", "with": "X"}, None)
    assert docs.canary_text is None


def test_a_no_op_after_the_canary_removes_it_and_writes_nothing(
        engine, monkeypatch, tmp_path, capsys):
    """У этой операции «текст уже такой» узнаётся только после карты. Это
    успешный ноль, а не отказ, и обещание «каждый отказ чистит канарейку»
    его не покрывало бы."""
    docs, _drive = _single(engine, monkeypatch)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Копия"}])
    assert code == 0
    note = out["op_notes"][0]
    assert note["applied_as"] == "no-op"
    assert "anchor_effects" not in note
    assert docs.canary_text is None
    assert not [b for b in docs.batches
                if any("deleteContentRange" in r for r in b[1:])]


# ------------------------------------------------------------ близнецы -----

def test_an_untrusted_twin_ordinal_is_refused_not_guessed(engine, monkeypatch,
                                                          tmp_path, capsys):
    """Физическое место у треда одно, а координат у него нет: маппер не
    смог доказать, какая из одинаковых копий его.

    Инвентаризация мест такой якорь считает существующим — и на этом
    резолвер не имеет права остановиться. Требование присутствия в
    доказанно размещённых якорях отдельное, и вот случай, где оно решает.
    """
    # выгрузка знает три «Копия», документ показывает две — порядковый номер
    # между чтениями не переносится
    docs, _drive = _stand(
        engine, monkeypatch, ["Копия", "Копия"],
        [("Копия", []), ("Копия", [("0", 0, 5)]), ("Копия", [])])
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"}])
    assert code == 3
    assert out["refused"][0]["reason"] in (
        "comment_thread_unresolvable", "anchor_identity_collision")
    assert docs.canary_text is None


def test_an_anchor_across_a_paragraph_break_is_refused(engine, monkeypatch,
                                                       tmp_path, capsys):
    """Комментарий, протянутый мышью через границу абзаца (#45), переписать
    нечем: удаление унесло бы саму границу и оформление второго абзаца."""
    from test_sync_anchors import _crossing_docx

    texts = ["Заголовок", "Подзаголовок", "Charlie"]
    docs = DocsStub(make_doc(texts))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _crossing_docx(docs, texts, "0", (0, 0), (1, len("Подзаголовок")),
                       [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"}])
    assert code == 3
    entry = out["refused"][0]
    assert entry["reason"] == "unsupported_structure"
    assert entry["details"]["construct"] == "paragraph_boundary"
    assert docs.canary_text is None


def test_the_export_and_the_document_must_agree_on_the_text(engine, capsys):
    """Выгрузка — это R0 плюс канарейка, снимок Docs — это R0. Тексты под
    комментарием обязаны сойтись.

    Пути, которым они разошлись бы, я не знаю: маппер ставит якорь, сверяя
    текст абзаца, — но источника здесь два, независимых, и цена молчания
    несимметрична. Разойдясь, они дадут запись мимо цели, а не отказ.
    Поэтому инвариант проверяется прямо, а тест зовёт резолвер напрямую:
    подделать расхождение через полный путь нечем.
    """
    tab = make_doc(["Alpha", "Копия", "Charlie"])
    snap = {"anchors": [(7, 12, "ДРУГОЕ", "0")], "attribution": {"0": "c1"},
            "thread_places": {"c1": 1}}
    with pytest.raises(SystemExit):
        engine._resolve_anchor_target(
            {"op": "replace_anchor", "comment_id": "c1", "with": "X"},
            tab, snap, None, "doc1")
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "concurrent_edit"
    assert payload["details"] == {"expected": "ДРУГОЕ", "found": "Копия"}


def test_a_second_anchor_in_another_tab_is_counted(engine, monkeypatch,
                                                   capsys):
    """Инвентаризация мест считается по ПОЛНОМУ списку якорей, до сведения к
    целевой вкладке.

    После сведения тред с якорем здесь и вторым якорем в соседней вкладке
    выглядит как тред с одним якорем — и правка по треду молча выбрала бы
    этот один. Найдено ревью codex.
    """
    from test_sync_anchors import make_docx_full
    from test_tabs import _Docs, _Drive, _Res, _tab_body

    def build_docx():
        paras = [("Черновик", []), ("ПОВТОР", [("0", 0, 6)]), ("", []),
                 ("Чистовик", []), ("ПОВТОР", [("1", 0, 6)]), ("", [])]
        if canary.get("text"):
            paras.append((canary["text"], []))
        return make_docx_full(paras, [("0", "Аня", "2026-08-18T10:00:00Z"),
                                      ("1", "Боря", "2026-08-18T11:00:00Z")])

    canary = {}
    doc = {"revisionId": "R0",
           "tabs": [_tab_body("t.0", "Черновик", ["ПОВТОР", ""]),
                    _tab_body("t.втор", "Чистовик", ["ПОВТОР", ""])]}
    # один разговор: корень Ани в первой вкладке, её ответ Бори — во второй
    comments = [{"id": "c0", "content": "раз", "author": {"displayName": "Аня"},
                 "createdTime": "2026-08-18T10:00:00Z",
                 "quotedFileContent": {"value": "ПОВТОР"},
                 "replies": [{"id": "r1", "createdTime":
                              "2026-08-18T11:00:00Z",
                              "author": {"displayName": "Боря"},
                              "action": "", "deleted": False}]}]

    class Docs(_Docs):
        def batchUpdate(self, documentId=None, body=None):
            self.batches.append(body)
            req = body["requests"][0]
            if "insertText" in req:
                canary["text"] = req["insertText"]["text"].lstrip("\n")
            return _Res({"writeControl": {"requiredRevisionId": "R1"}})

    docs = Docs(doc)
    drive = _Drive(comments, None)
    drive.export = lambda fileId=None, mimeType=None: _Res(build_docx())
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    _all, anchored, fp1, universe = engine._census_comments(drive, "F")
    target = doc["tabs"][1]["documentTab"]
    snap, why = engine._fresh_anchor_snapshot(
        docs, drive, "F", doc, target, anchored, [],
        target["body"]["content"][-1]["endIndex"],
        fp1=fp1, universe=universe, tid="t.втор")
    assert why is None, why
    # в целевой вкладке размещён ОДИН якорь, а мест у треда во всём
    # документе — два, и решает именно это
    assert len(snap["anchors"]) == 1
    assert snap["thread_places"]["c0"] == 2
    with pytest.raises(SystemExit):
        engine._resolve_anchor_target(
            {"op": "replace_anchor", "comment_id": "c0", "with": "X"},
            target, snap, "t.втор", "F")
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "comment_thread_has_multiple_anchors"
    assert payload["details"]["anchors"] == 2


def test_a_deferred_op_carrying_a_bare_ordinal_is_refused(engine, monkeypatch,
                                                          tmp_path, capsys):
    """Отложенная операция живёт уникальностью: на момент записи цель у неё
    ровно одна, и это доказательство адреса.

    С ЯВНЫМ номером вхождения доказательства нет — номер осмыслен только
    вместе с числом копий, а плановая фаза его не видела, она эту операцию
    отвергла. Применить голый порядковый номер к состоянию документа,
    которого человек не видел, нельзя.
    """
    texts = ["Копия", "Charlie"]
    _docs, _drive = _stand(
        engine, monkeypatch, texts,
        [("Копия", [("0", 0, 5)]), ("Charlie", [])], mutating=True)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Копия"},
        # на исходном снимке копий одна, значит второй не существует
        {"op": "replace_quote", "quote": "Копия", "occurrence": 2,
         "with": "Вторая"},
    ])
    assert code == 3
    entry = out["refused"][0]
    assert entry["reason"] == "concurrent_edit"
    assert "номер вхождения имеет смысл только" in entry["error"]


# --------------------------------------- находки ревью кода (codex, r2) ----

def test_an_insert_before_it_poisons_the_thread_address(engine, monkeypatch,
                                                        tmp_path, capsys):
    """Вставка карту комментариев не строит и потому не может сказать,
    попала ли она внутрь чьего-то якоря.

    Пропустить после неё правку по треду значит позволить ей молча накрыть
    результат соседки: вставка «оп» → «опX» внутрь якоря меняет текст под
    комментарием, но в списке задетых тредов её нет и быть не может.
    Найдено ревью codex — тест на обычной замене эту дыру не видел, потому
    что обычная замена карту строит.
    """
    _docs, _drive = _single(engine, monkeypatch, mutating=True)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "insert_after_quote", "quote": "оп", "text": "X"},
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"},
    ])
    assert code == 3
    assert out["ops_applied"] == 1
    entry = out["refused"][0]
    assert entry["reason"] == "concurrent_edit"
    assert "карту комментариев не строила" in entry["error"]


def test_the_same_file_the_other_way_round_goes_through(engine, monkeypatch,
                                                        tmp_path, capsys):
    """Зеркало: правка по треду ПЕРЕД вставкой проходит.

    Без этой половины предыдущий тест доказывал бы, что вставка и правка по
    треду несовместимы вовсе, а совет «поставьте правки по тредам раньше»
    был бы неправдой.

    Вставка адресуется текстом, которого на исходном снимке ещё не было, —
    то есть заодно проверяется отложенный путь: цель второй операции создана
    первой и разрешается по живому документу (#36).
    """
    _docs, _drive = _single(engine, monkeypatch, mutating=True)
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Правка"},
        {"op": "insert_after_quote", "quote": "Правка", "text": " ещё"},
    ])
    assert code == 0, json.dumps(out, ensure_ascii=False)[:900]
    assert out["ops_applied"] == 2
    assert "deferred" in out["op_notes"][1]


def test_a_no_op_is_recognised_before_the_gates_run(engine, monkeypatch,
                                                    tmp_path, capsys):
    """Писать нечего — значит и отказывать не за что.

    Равная по смыслу обычная замена возвращает честный ноль ещё до проверки
    предложений; правка по треду не должна вести себя иначе только потому,
    что её цель разрешается позже. Найдено ревью codex.
    """
    docs, _drive = _single(engine, monkeypatch)
    para = docs.base["body"]["content"][1]
    para["paragraph"]["elements"][0]["textRun"]["suggestedInsertionIds"] = \
        ["sug1"]
    code, out = _run(engine, tmp_path, capsys, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Копия"}])
    assert code == 0
    assert out["op_notes"][0]["applied_as"] == "no-op"
    assert docs.canary_text is None


def test_a_failed_cleanup_is_never_swallowed(engine, monkeypatch, capsys):
    """Неудачная уборка канарейки обязана дойти до человека.

    Для отказа она уходит в текст ошибки; для любой другой аварии — отдельным
    предупреждением, иначе служебная строка остаётся в документе, и об этом
    никто не узнает.
    """
    docs, drive = _single(engine, monkeypatch)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    monkeypatch.setattr(engine, "_cleanup_canary", lambda *a, **kw: False)

    def boom(*a, **kw):
        raise RuntimeError("резолвер упал")

    monkeypatch.setattr(engine, "_resolve_anchor_target", boom)
    with pytest.raises(RuntimeError):
        engine._apply_op_anchor_safe(docs, drive, "doc1", {
            "op": "replace_anchor", "comment_id": "c1", "with": "X"}, None)
    warning = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert "осталась служебная строка" in warning["warning"]
