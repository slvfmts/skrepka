"""Точный автоматический ответ (T10).

Правило владельца: якорь сажается на соседнее слово И В ТРЕД ПИШЕТСЯ, что
произошло. Пересадка — единственный случай, которого человек увидеть не может:
комментарий стоит уже не на том слове, о котором он писал.

В 0.10 автоответы уже были и их удалили (#22) — не за идею, а за то, что они
считали эффект по устаревшей цитате `quotedFileContent` и называли текст,
которого правка не касалась. Здесь текст строится ТОЛЬКО из подтверждённого
свежего эффекта, а отправка идёт общим слоем T6 со шлюзом и пост-проверкой.
"""
import json
import os

import pytest

from test_sync_anchors import (
    CREATED,
    CREATED_SEC,
    DriveStub,
    MutatingDocsStub,
    _docx_builder,
    api_comment,
    make_doc,
    wire,
)

PARA = "Мы обсудили ЛИШНЕЕ слово подробно"
A_OFF = (12, 18)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Конфиг в поддельном HOME: блокировка документа берётся настоящая."""
    os.chmod(tmp_path, 0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKREPKA_CONFIG_DIR", str(tmp_path / "cfg"))
    import skrepka.config as config
    config.ensure_config_dir()
    return config


def _note(effects, basis="export-map", unknown=()):
    note = {"applied_as": "reseated", "effects_basis": basis,
            "anchor_effects": list(effects)}
    if unknown:
        note["unknown_effect_comment_ids"] = list(unknown)
    return note


def _eff(cid, before="ЛИШНЕЕ", after="слово", effect="reseated"):
    return {"comment_id": cid, "text_before": before, "text_after": after,
            "effect": effect}


def _stand(engine, monkeypatch, texts=(PARA,), paras=None):
    doc = make_doc(list(texts))
    docs = MutatingDocsStub(doc)
    drive = DriveStub([api_comment("c1", "A", CREATED)],
                      _docx_builder(docs, paras or [(PARA, [("0", *A_OFF)])],
                                    [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    return docs, drive


def _run(engine, tmp_path, capsys, ops, name="ops.json"):
    path = tmp_path / name
    path.write_text(json.dumps(ops), encoding="utf-8")
    code = 0
    try:
        engine.patch_doc("doc1", str(path))
    except SystemExit as exc:
        code = exc.code
    return code, json.loads(capsys.readouterr().out), str(path)


def _around(cid="c1", before="Мы кратко обсудили ", after="слово подробно"):
    return {"op": "replace_around_anchor", "comment_id": cid, "quote": PARA,
            "with": {"before": before, "after": after}}


# ------------------------------------------------------------------ текст --

def test_the_reply_names_both_words(engine):
    """Две вещи, которых человек не знает: что убрано и где теперь его
    комментарий."""
    assert engine._intent_text("ЛИШНЕЕ", "слово") == (
        "Убрал «ЛИШНЕЕ». Ваш комментарий теперь на соседнем слове — «слово».")


@pytest.mark.parametrize("before, after", [
    ("", "слово"), ("ЛИШНЕЕ", ""), (None, "слово"), ("ЛИШНЕЕ", None),
])
def test_without_exact_words_there_is_no_reply_at_all(engine, before, after):
    """Приблизительный ответ запрещён: ровно на приблизительном и построили
    автоответы 0.10, которые пришлось удалить."""
    assert engine._intent_text(before, after) is None


def test_a_long_removed_text_is_not_quoted(engine):
    """Длинная фраза в кавычках читается плохо, а ответ в чужом документе
    обязан читаться."""
    long = "о" * 41
    got = engine._intent_text(long, "слово")
    assert got.startswith("Убрал то, о чём вы писали.")
    assert long not in got
    assert engine._intent_text("о" * 40, "слово").startswith("Убрал «")


@pytest.mark.parametrize("removed", ["с «кавычкой» внутри", "две\nстроки"])
def test_a_quote_inside_a_quote_switches_to_the_general_wording(engine,
                                                                removed):
    assert engine._intent_text(removed, "слово").startswith(
        "Убрал то, о чём вы писали.")


def test_a_neighbour_spelled_the_same_does_not_read_as_a_contradiction(engine):
    """«Убрал «слово». Комментарий теперь на слове «слово»» выглядит ошибкой,
    хотя формально верно."""
    assert engine._intent_text("слово", "слово") == (
        "Убрал «слово». Ваш комментарий теперь на соседнем таком же слове.")


# ---------------------------------------------------------------- свёртка --

def test_only_a_reseat_earns_a_reply(engine):
    """Остальные эффекты человек видит сам, открыв тред."""
    for effect in ("edited", "extended", "dropped", "rewritten"):
        assert engine._fold_reply_intents(
            [_note([_eff("c1", effect=effect)])])[0] == []
    assert engine._fold_reply_intents([_note([_eff("c1")])])[0]


def test_a_later_known_effect_displaces_an_earlier_one(engine):
    """Ранняя квитанция была правдива в момент применения, но к концу прогона
    «теперь на слове X» уже ложь — там другое."""
    got, _sup = engine._fold_reply_intents([
        _note([_eff("c1")]),
        _note([_eff("c1", effect="edited")]),
    ])
    assert got == []


def test_an_unmapped_write_cancels_everything_before_it(engine):
    """`not-mapped` буквально значит, что связь с якорями не устанавливалась.
    Локализовать такую операцию по диапазону нельзя — это была бы новая карта
    эффектов, то есть новое допущение без замера."""
    got, suppressed = engine._fold_reply_intents([
        _note([_eff("c1")]),
        _note([], basis="not-mapped"),
    ])
    assert got == []
    # промолчать в треде правильно, промолчать в квитанции — нет
    assert [s["comment_id"] for s in suppressed] == ["c1"]


def test_an_effect_measured_AFTER_an_unmapped_write_survives(engine):
    """Оно считалось по свежей карте, которая результат вставки уже включает.
    Отменять его значило бы молчать там, где мы знаем."""
    got, suppressed = engine._fold_reply_intents([
        _note([], basis="not-mapped"),
        _note([_eff("c2")]),
    ])
    assert [c for c, _t in got] == ["c2"] and suppressed == []


def test_an_unknown_write_outcome_cancels_all_intents(engine):
    """Что в документе — больше не известно, и говорить про него нечего."""
    got, suppressed = engine._fold_reply_intents(
        [_note([_eff("c1")])], write_outcome_unknown=True)
    assert got == [] and [s["comment_id"] for s in suppressed] == ["c1"]


def test_a_closed_thread_never_gets_a_reply(engine):
    """Выгрузка их не показывает вовсе, и утверждать про них нечего."""
    got, _sup = engine._fold_reply_intents([
        _note([_eff("c1")], basis="export-map-open-threads-only",
              unknown=["c1"])])
    assert got == []


# ----------------------------------------------------------------- outbox --

def test_the_outbox_is_written_BEFORE_the_first_reply(
        engine, monkeypatch, tmp_path, capsys, store):
    """Процесс, умерший ПОСЛЕ `replies.create`, но до записи файла, не оставил
    бы следа вовсе — и повторный `patch` либо потерял бы обязательный ответ,
    либо создал второй."""
    seen = []
    real = engine._write_pending_outbox
    monkeypatch.setattr(engine, "_write_pending_outbox",
                        lambda p, i: (seen.append("outbox"), real(p, i))[1])
    _docs, drive = _stand(engine, monkeypatch)
    real_create = drive.create
    monkeypatch.setattr(drive, "create",
                        lambda **kw: (seen.append("create"),
                                      real_create(**kw))[1])
    _run(engine, tmp_path, capsys, [_around()])
    assert seen[0] == "outbox" and "create" in seen


def test_a_foreign_file_in_the_way_is_never_overwritten(engine, tmp_path):
    """Перезапись уничтожила бы чужую очередь или свидетельство прошлого
    прогона."""
    path = str(tmp_path / "чужой.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("не трогать")
    got, err = engine._write_pending_outbox(path, [("c1", "текст")])
    assert got is None and "не создать" in err
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == "не трогать"


def test_two_runs_never_share_an_outbox(engine, tmp_path):
    """Одинаковые треды с одинаковым текстом не означают ту же работу. Старый
    журнал объявил бы ответы уже применёнными, и новая обязательная запись не
    ушла бы."""
    same = [("c1", "текст")]
    a = engine._pending_outbox_path("ops.json", "doc1", "run-a", same)
    b = engine._pending_outbox_path("ops.json", "doc1", "run-b", same)
    other_doc = engine._pending_outbox_path("ops.json", "doc2", "run-a", same)
    assert a != b and a != other_doc


# ------------------------------------------------------------ через patch --

def test_the_thread_gets_the_reply_and_the_receipt_names_it(
        engine, monkeypatch, tmp_path, capsys, store):
    """Ради этого задача и заведена."""
    _docs, drive = _stand(engine, monkeypatch)
    code, out, _path = _run(engine, tmp_path, capsys, [_around()])
    assert code == 0, json.dumps(out, ensure_ascii=False)[:1200]
    assert drive.replies_created == [
        ("c1", "Убрал «ЛИШНЕЕ». Ваш комментарий теперь на соседнем слове — "
               "«слово».")]
    auto = out["auto_replies"]
    assert auto["replies_sent"] == 1
    assert [r["state"] for r in auto["replies"]] == ["applied"]
    assert auto["replies"][0]["comment_id"] == "c1"


def test_a_successful_run_stops_calling_the_file_pending(
        engine, monkeypatch, tmp_path, capsys, store):
    """Имя, пережившее свой смысл, вводит в заблуждение так же, как молчание."""
    _stand(engine, monkeypatch)
    _code, out, _p = _run(engine, tmp_path, capsys, [_around()])
    auto = out["auto_replies"]
    # файл НЕ переименовывается: рядом лежит журнал под своим именем, и
    # переименованный оказался бы валидным `replies.json` без журнала — то
    # есть `reply --file` отправил бы всё повторно
    assert ".skrepka-auto-replies." in auto["outbox"]
    assert os.path.exists(auto["outbox"])
    assert auto["complete"] is True and "resume" not in auto


def test_an_unknown_reply_outcome_is_never_offered_for_resume(
        engine, monkeypatch, tmp_path, capsys, store):
    """Связь оборвалась ПОСЛЕ отправки: ушёл ответ или нет — неизвестно.
    Предложить дослать его значит предложить написать в чужой документ
    второй раз."""
    _docs, drive = _stand(engine, monkeypatch)
    monkeypatch.setattr(drive, "create",
                        lambda **kw: (_ for _ in ()).throw(
                            RuntimeError("сеть отвалилась")))
    code, out, _p = _run(engine, tmp_path, capsys, [_around()])
    auto = out["auto_replies"]
    assert code == 3 and out["text_applied_reply_pending"] is True
    assert out["action"] == "partially-patched"
    assert [r["state"] for r in auto["replies"]] == ["unknown"]
    assert "resume" not in auto and auto["complete"] is False


def test_replies_never_attempted_get_a_file_and_a_command_with_the_flag(
        engine, monkeypatch, tmp_path, capsys, store):
    """Отправка не начиналась вовсе — вот это дослать можно и нужно. Команда
    несёт `--include-foreign`: без него пакетный `reply` пропустил бы
    заказчицкий тред как чужой, и обязательный ответ молча не ушёл бы."""
    _docs, drive = _stand(engine, monkeypatch)
    monkeypatch.setattr(engine, "_my_identity", lambda d, raw=(): (None, None))
    code, out, _p = _run(engine, tmp_path, capsys, [_around()])
    auto = out["auto_replies"]
    assert code == 3 and drive.replies_created == []
    assert auto["stopped_because"] == "reply_identity_unknown"
    assert [r["state"] for r in auto["replies"]] == ["not_attempted"]
    assert auto["resume"][-1] == "--include-foreign"
    assert os.path.exists(auto["outbox"])
    with open(auto["outbox"], encoding="utf-8") as fh:
        assert json.load(fh)["replies"][0]["comment_id"] == "c1"


def test_a_terminally_skipped_thread_is_not_offered_for_resume(
        engine, monkeypatch, tmp_path, capsys, store):
    """Закрытый тред повторять нельзя, и звать к этому командой значит звать
    сделать вред."""
    closed = api_comment("c1", "A", CREATED, resolved=True)
    _docs, _drive = _switching_stand(engine, monkeypatch, [closed])
    _code, out, _p = _run(engine, tmp_path, capsys, [_around()])
    assert "resume" not in out["auto_replies"]


def test_the_text_is_not_rolled_back_when_the_reply_fails(
        engine, monkeypatch, tmp_path, capsys, store):
    """Текст уже в документе, и отменять его нельзя — это была бы вторая
    правка чужого документа поверх первой."""
    docs, drive = _stand(engine, monkeypatch)
    monkeypatch.setattr(drive, "create",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("бум")))
    _run(engine, tmp_path, capsys, [_around()])
    texts = [el["paragraph"]["elements"][0]["textRun"]["content"][:-1]
             for el in docs.base["body"]["content"] if "paragraph" in el]
    assert texts == ["Мы кратко обсудили слово подробно"]


def test_a_plain_replace_earns_no_reply(engine, monkeypatch, tmp_path, capsys,
                                        store):
    """Просьба, выполненная дословно, ответа не требует (STANDARD §8)."""
    _docs, drive = _stand(engine, monkeypatch)
    code, out, _p = _run(engine, tmp_path, capsys, [
        {"op": "replace_quote", "quote": "подробно", "with": "кратко"}])
    assert code == 0 and drive.replies_created == []
    assert "auto_replies" not in out


def test_a_refused_operation_earns_no_reply(engine, monkeypatch, tmp_path,
                                            capsys, store):
    _docs, drive = _stand(engine, monkeypatch)
    code, _out, _p = _run(engine, tmp_path, capsys, [
        {"op": "replace_around_anchor", "comment_id": "нет-такого",
         "quote": PARA, "with": {"before": "а ", "after": "б"}}])
    assert code == 3 and drive.replies_created == []


def test_the_author_of_the_thread_does_not_matter(
        engine, monkeypatch, tmp_path, capsys, store):
    """Операция назвала тред явно — это и есть разрешение. Комментарии в работе
    заводит заказчик, и фильтр по автору отрезал бы главный сценарий."""
    doc = make_doc([PARA])
    docs = MutatingDocsStub(doc)
    foreign = api_comment("c1", "Заказчик", CREATED)
    foreign["author"]["me"] = False
    drive = DriveStub([foreign],
                      _docx_builder(docs, [(PARA, [("0", *A_OFF)])],
                                    [("0", "Заказчик", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    code, _out, _p = _run(engine, tmp_path, capsys, [_around()])
    assert code == 0 and len(drive.replies_created) == 1


# ---------------------------------------------------- находки стенда мутаций

def test_every_text_write_happens_before_any_reply(
        engine, monkeypatch, tmp_path, capsys, store):
    """Порядок не косметический: сорвись запись на середине — никто не должен
    быть уведомлён о том, чего не случилось."""
    order = []
    docs, drive = _stand(engine, monkeypatch, texts=(PARA, "Хвост абзаца"))
    real_batch = docs.documents().batchUpdate
    real_create = drive.create
    monkeypatch.setattr(
        engine, "_send_reply_intents",
        lambda d, f, o, i: (order.append("reply"),
                            {"replies": [], "replies_sent": 0})[1])
    real_apply = engine._apply_op_anchor_safe
    monkeypatch.setattr(
        engine, "_apply_op_anchor_safe",
        lambda *a, **kw: (order.append("text"), real_apply(*a, **kw))[1])
    _run(engine, tmp_path, capsys, [
        _around(),
        {"op": "replace_quote", "quote": "Хвост абзаца", "with": "Другой"}])
    assert order == ["text", "text", "reply"], order
    assert real_batch is not None and real_create is not None


def _switching_stand(engine, monkeypatch, after):
    """Перепись меняется между текстовой частью и ответами — так выглядит
    человек, закрывший или удаливший тред в это самое время."""
    doc = make_doc([PARA])
    docs = MutatingDocsStub(doc)
    drive = DriveStub([api_comment("c1", "A", CREATED)],
                      _docx_builder(docs, [(PARA, [("0", *A_OFF)])],
                                    [("0", "A", CREATED_SEC)]),
                      comments_after=after, switch_after_lists=3)
    wire(engine, monkeypatch, docs, drive)
    return docs, drive


def test_a_thread_resolved_between_text_and_reply_gets_nothing(
        engine, monkeypatch, tmp_path, capsys, store):
    """Тред закрыл человек. Писать в закрытый разговор нельзя, а переоткрывать
    его ответом тем более: закрытие — его решение."""
    closed = api_comment("c1", "A", CREATED, resolved=True)
    _docs, drive = _switching_stand(engine, monkeypatch, [closed])
    code, out, _p = _run(engine, tmp_path, capsys, [_around()])
    assert drive.replies_created == []
    assert [r["state"] for r in out["auto_replies"]["replies"]] == \
        ["skipped_resolved"]
    assert code == 3 and out["text_applied_reply_pending"] is True


def test_a_thread_deleted_between_text_and_reply_gets_nothing(
        engine, monkeypatch, tmp_path, capsys, store):
    _docs, drive = _switching_stand(engine, monkeypatch, [])
    code, out, _p = _run(engine, tmp_path, capsys, [_around()])
    assert drive.replies_created == []
    assert [r["state"] for r in out["auto_replies"]["replies"]] == \
        ["skipped_missing"]
    assert code == 3


def test_the_journal_of_an_auto_reply_is_opened_for_foreign_threads(
        engine, monkeypatch, tmp_path, capsys, store):
    """Возобновление руками пойдёт обычным `reply --file`. Заведи журнал в
    режиме «только свои» — политика пакетного T6 пропустила бы заказчицкий
    тред как чужой, и обязательный ответ молча не ушёл бы."""
    _stand(engine, monkeypatch)
    _code, out, _p = _run(engine, tmp_path, capsys, [_around()])
    with open(out["auto_replies"]["reply_journal"], encoding="utf-8") as fh:
        header = json.load(fh)["header"]
    assert header["include_foreign"] is True
    assert header["run_id"]


def test_an_unknown_write_outcome_cancels_the_replies_of_this_run(
        engine, monkeypatch, tmp_path, capsys, store):
    """Первая операция пересадила комментарий и была правдиво описана. Вторая
    оборвалась с неизвестным исходом — что теперь в документе, мы не знаем, и
    говорить заказчику «ваш комментарий теперь на слове X» нельзя."""
    _docs, drive = _stand(engine, monkeypatch, texts=(PARA, "Хвост абзаца"))
    real_apply = engine._apply_op_anchor_safe
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_apply(*a, **kw)
        raise engine.PatchOpError("связь оборвалась после отправки",
                                  state="unknown")

    monkeypatch.setattr(engine, "_apply_op_anchor_safe", flaky)
    code, out, _p = _run(engine, tmp_path, capsys, [
        _around(),
        {"op": "replace_quote", "quote": "Хвост абзаца", "with": "Другой"}])
    assert code == 3
    assert out["failed_op_state"] == "unknown"
    assert drive.replies_created == []
    # В треде молчим — это правильно. Но в квитанции обязаны назвать поимённо,
    # что обязательный ответ не выполнен: иначе молчание сойдёт за успех.
    assert [x["comment_id"] for x in out["auto_replies"]["suppressed"]] == ["c1"]
    assert out["auto_replies"]["complete"] is False


def test_any_control_character_switches_to_the_general_wording(engine):
    """Не только перевод строки: табуляция, возврат каретки и управляющие
    знаки направления попали бы в чужой документ дословно."""
    for bad in ("таб\tвнутри", "возврат\rкаретки", "напр‮авление"):
        assert engine._intent_text(bad, "слово").startswith(
            "Убрал то, о чём вы писали.")


def test_the_auto_reply_journal_can_be_resumed_by_the_batch_command(
        engine, tmp_path, store):
    """Автоответ отправлял БЕЗ фильтра по автору: каждый тред был назван
    операцией явно. `--include-foreign` один этого не воспроизводит — он
    разрешает доказанно чужой тред, но не «Google не сказал». Поэтому режим
    назван в журнале и учитывается при возобновлении именно его."""
    path = str(tmp_path / "outbox.json")
    h = engine._reply_input_hash([("c1", "текст")])
    with open(engine._reply_journal_path(path), "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "pid-1", "include_foreign": True,
            "explicit_comment_ids": True, "input_sha256": h},
            "replies": []}, fh)
    got, status = engine._read_reply_journal(
        engine._reply_journal_path(path), "doc1", "pid-1", h, True)
    assert status == "valid" and got is not None


def test_resuming_an_auto_reply_journal_ignores_the_author_filter(
        engine, monkeypatch, tmp_path, capsys, store):
    """Автоответ отправлял без фильтра: каждый тред был назван операцией явно.
    Возобновляя ИМЕННО его журнал, фильтр применять нельзя — иначе
    обязательный ответ молча не уйдёт в тред, про который Google не сказал,
    чей он."""
    from test_sync_anchors import DriveStub as _D
    doc = make_doc([PARA])
    docs = MutatingDocsStub(doc)
    unknown = api_comment("c1", "Заказчик", CREATED)
    unknown["author"].pop("me", None)          # Google не сказал, чей тред
    drive = _D([unknown], _docx_builder(docs, [(PARA, [("0", *A_OFF)])],
                                        [("0", "Заказчик", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    path = str(tmp_path / "outbox.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"replies": [{"comment_id": "c1", "text": "дослать"}]}, fh)
    h = engine._reply_input_hash([("c1", "дослать")])
    with open(engine._reply_journal_path(path), "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "pid-1", "include_foreign": True,
            "explicit_comment_ids": True, "input_sha256": h},
            "replies": []}, fh)
    code = 0
    try:
        engine.batch_reply("doc1", path, include_foreign=True)
    except SystemExit as exc:
        code = exc.code
    out = json.loads(capsys.readouterr().out)
    assert drive.replies_created == [("c1", "дослать")], json.dumps(
        out, ensure_ascii=False)[:600]
    assert code == 0


def test_a_stop_after_an_applied_reply_is_not_a_full_success(
        engine, monkeypatch, tmp_path, capsys, store):
    """Ответ ушёл, но отправка встала — например, он отнял у соседнего треда
    последнюю примету, и документ мог оказаться заперт. Считать это полным
    успехом значит скрыть самое опасное."""
    _stand(engine, monkeypatch)
    real = engine._reply_send_all
    monkeypatch.setattr(
        engine, "_reply_send_all",
        lambda *a, **kw: (real(*a, **kw), "identity_collision_created")[1])
    code, out, _p = _run(engine, tmp_path, capsys, [_around()])
    auto = out["auto_replies"]
    assert auto["replies_sent"] == 1          # ответ ушёл
    assert auto["complete"] is False          # но успехом это не назовёшь
    assert auto["stopped_because"] == "identity_collision_created"
    assert code == 3 and out["text_applied_reply_pending"] is True


def test_a_broken_lock_or_census_does_not_swallow_the_patch_receipt(
        engine, monkeypatch, tmp_path, capsys, store):
    """Текст уже в документе. Уронить процесс на фазе ответов значит потерять
    квитанцию обо всём, что применено."""
    _docs, drive = _stand(engine, monkeypatch)

    def no_lock(name=None):
        raise engine.config.ConfigError("блокировка недоступна")

    monkeypatch.setattr(engine.config, "lock", no_lock)
    code, out, _p = _run(engine, tmp_path, capsys, [_around()])
    assert drive.replies_created == []
    assert code == 3
    assert out["ops_applied"] == 1 and out["op_notes"]
    assert out["auto_replies"]["stopped_because"] == "reply_phase_failed"


def test_a_journal_that_cannot_be_written_does_not_kill_the_receipt(
        engine, monkeypatch, tmp_path, capsys, store):
    """Пакетный `reply` без журнала работать не может и честно падает. У
    автоответа текст уже применён, и его квитанция важнее."""
    _stand(engine, monkeypatch)
    # Ломаем НАСТОЯЩУЮ запись, а не подменяем `_reply_journal_write`: иначе
    # ветка «падать или вернуть ошибку» не выполняется вовсе, и тест перестаёт
    # различать два поведения (поймано стендом мутаций).
    import skrepka.safeio as safeio
    real_write = safeio.atomic_write

    def boom(path, data, **kw):
        if ".skrepka-auto-replies." in path:
            raise safeio.SafeIOError("диск полон")
        return real_write(path, data, **kw)

    monkeypatch.setattr(engine.safeio, "atomic_write", boom)
    code, out, _p = _run(engine, tmp_path, capsys, [_around()])
    assert code == 3
    assert out["ops_applied"] == 1
    assert "journal_write_failed" in out["auto_replies"]["stopped_because"]


def test_a_selection_that_grabbed_a_space_is_quoted_without_it(engine):
    """Выделение человека почти всегда прихватывает пробел с краю. Живой автор
    так не процитирует — «Убрал «НЕНУЖНОГО »» найдено живой приёмкой."""
    assert engine._intent_text("НЕНУЖНОГО ", "вывода") == (
        "Убрал «НЕНУЖНОГО». Ваш комментарий теперь на соседнем слове — "
        "«вывода».")
    assert engine._intent_text(" две слова ", "х").startswith(
        "Убрал «две слова».")


def test_a_selection_of_only_spaces_earns_no_reply(engine):
    """«Убрал «»» выглядит поломкой, а не ответом."""
    assert engine._intent_text("   ", "слово") is None
