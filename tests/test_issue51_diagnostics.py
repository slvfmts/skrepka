"""Отказ несёт машиночитаемый диагноз и правильный выход (#51, r19/T5).

Строка для человека остаётся первичной: всё, что читало квитанции раньше,
продолжает работать. Рядом с ней появляются `reason` — устойчивый код — и
`details` с тем, что можно проверить глазами.

Отдельно здесь живёт регрессия M27: два ответа в одну секунду запирают замены
во всём документе, и раньше отказ советовал разбираться с призраками, которых
в документе нет. Разбор уходил в их поиск (пост-мортем 20 августа).
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


# --- база: код и детали доезжают обоими путями ----------------------------

def test_error_emits_the_code_and_the_details(engine, capsys):
    with pytest.raises(SystemExit):
        engine._error("что-то не так", reason="quote_not_found",
                      details={"quote": "абв"})
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "что-то не так"
    assert out["reason"] == "quote_not_found"
    assert out["details"] == {"quote": "абв"}


def test_error_without_a_code_stays_exactly_as_it_was(engine, capsys):
    """Обратная совместимость: пустых полей в квитанции не появляется."""
    with pytest.raises(SystemExit):
        engine._error("просто ошибка")
    out = json.loads(capsys.readouterr().out)
    assert out == {"error": "просто ошибка"}


def test_per_op_mode_carries_the_code_on_the_exception(engine):
    engine._RAISE_ERRORS = True
    try:
        with pytest.raises(engine.PatchOpError) as exc:
            engine._error("нельзя", reason="concurrent_edit",
                          details={"expected": "а", "found": "б"})
    finally:
        engine._RAISE_ERRORS = False
    assert exc.value.reason == "concurrent_edit"
    assert exc.value.details == {"expected": "а", "found": "б"}
    assert exc.value.state == "not_applied"


def test_an_unknown_code_is_a_programming_error(engine):
    """Коды — контракт. Опечатка в коде должна падать у нас, а не молча
    доезжать до агента, который на этот код смотрит.

    Именно `raise`, а не `assert`: под `python -O` ассерты исчезают, и
    проверка публичного контракта пропала бы ровно там, где её нельзя
    терять (codex, ревью r19)."""
    with pytest.raises(ValueError):
        engine._error("что-то", reason="ой_я_придумал_новый")


def test_the_code_contract_survives_optimised_python(tmp_path):
    """Отдельным процессом с -O: иначе тест выше закрепляет только обычный
    режим, а весь смысл замены assert на raise — в оптимизированном."""
    import subprocess
    import sys as _sys

    script = tmp_path / "check.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, 'src')\n"
        "from skrepka import _engine\n"
        "try:\n"
        "    _engine._error('x', reason='нет-такого-кода')\n"
        "except ValueError:\n"
        "    print('RAISED')\n"
        "except SystemExit:\n"
        "    print('LEAKED')\n",
        encoding="utf-8")
    out = subprocess.run([_sys.executable, "-O", str(script)],
                         capture_output=True, text=True,
                         cwd="/Users/slava/dev/skrepka")
    assert "RAISED" in out.stdout, out.stdout + out.stderr


def test_details_are_bounded(engine, capsys):
    """Квитанцию читают и человек, и агент: цитата на десять килобайт — это
    не диагностика, а шум, в котором тонет причина."""
    with pytest.raises(SystemExit):
        engine._error("длинно", reason="quote_not_found",
                      details={"quote": "я" * 5000,
                               "matches": ["ы" * 5000] * 40})
    out = json.loads(capsys.readouterr().out)
    assert len(out["details"]["quote"]) == engine._DETAILS_CAP + 1
    assert len(out["details"]["matches"]) == 10


def test_every_declared_code_is_snake_case_and_stable(engine):
    assert engine._REASON_CODES
    for code in engine._REASON_CODES:
        assert code.islower() and " " not in code


# --- отказ по операции доносит код до квитанции ---------------------------

def _stand(engine, monkeypatch, comments, docx_comments, paras):
    doc = make_doc([p[0] for p in paras])
    docs = DocsStub(doc)
    drive = DriveStub(comments, _docx_builder(docs, paras, docx_comments))
    wire(engine, monkeypatch, docs, drive)
    return docs, drive


def test_the_receipt_carries_the_code_of_a_refused_operation(engine,
                                                             monkeypatch,
                                                             tmp_path, capsys):
    _stand(engine, monkeypatch,
           [api_comment("c1", "A", CREATED)],
           [("0", "A", CREATED_SEC)],
           [("Alpha", []), ("Bravo", [("0", 0, 5)]), ("Charlie", [])])
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "нет такого текста", "with": "X"},
        {"op": "replace_quote", "quote": "Charlie", "with": "Xray"},
    ]), encoding="utf-8")
    with pytest.raises(SystemExit):
        engine.patch_doc("doc1", str(ops))
    out = json.loads(capsys.readouterr().out)
    assert out["ops_applied"] == 1
    refused = out["refused"][0]
    assert refused["reason"] == "quote_not_found"
    assert refused["details"]["quote"] == "нет такого текста"


# --- регрессия M27 --------------------------------------------------------

def test_two_replies_in_one_second_are_named_by_their_own_reason(engine,
                                                                 monkeypatch,
                                                                 tmp_path,
                                                                 capsys):
    """Замерено на живом документе (M27): два треда, получившие запись одного
    автора в одну секунду, неразличимы в выгрузке, и замены останавливаются
    во всём файле — включая абзацы без комментариев.

    Отказ обязан называть ЭТУ причину и ЭТОТ выход. Раньше он приезжал с
    советом разобраться с комментариями-призраками, которых в документе нет.
    """
    _stand(engine, monkeypatch,
           [api_comment("c1", "A", CREATED), api_comment("c2", "A", CREATED)],
           [("0", "A", CREATED_SEC), ("1", "A", CREATED_SEC)],
           [("Alpha", [("0", 0, 5)]), ("Bravo", [("1", 0, 5)]),
            ("Charlie", [])])
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Charlie", "with": "Xray"}]),
        encoding="utf-8")
    with pytest.raises(SystemExit):
        engine.patch_doc("doc1", str(ops))
    out = json.loads(capsys.readouterr().out)
    err = out["refused"][0]
    assert err["reason"] == "anchor_identity_collision"
    # Совет обязан РАЗБЛОКИРОВАТЬ, а не просто упомянуть секунду. Ответ в
    # один тред делает уникальным только его: у второго все ключи остаются
    # общими с первым, и он по-прежнему в проблемах. Значит отвечать надо в
    # каждый столкнувшийся тред, по одному (codex, ревью r19).
    assert "КАЖДЫЙ" in err["error"]
    assert "по одному" in err["error"]
    # и ни слова про призраков и работу руками
    for forbidden in ("призрак", "в UI", "в интерфейсе"):
        assert forbidden not in err["error"]


def test_the_collision_advice_does_not_leak_into_other_reasons(engine):
    """Совет про секунду верен ровно для своего случая. Один совет на все
    причины — это дефект #24, ради которого задача и заведена."""
    other = engine._anchor_map_remedy(
        "anchor 0 matches 2 paragraphs and one of them cannot hold it")
    assert "секунду" not in other


def test_the_advice_actually_unblocks_the_document(engine, monkeypatch,
                                                   tmp_path, capsys):
    """Совет обязан работать, а не звучать правдоподобно.

    Здесь тот же документ, но каждый из столкнувшихся тредов получил свой
    ответ в СВОЮ секунду — ровно то, что предписывает отказ. Учёт после этого
    сходится, и правка проходит.

    Тест заведён по ревью r19: первая редакция совета предлагала ответить в
    один тред из двух, и это не разблокировало бы — у второго все ключи
    остались бы общими с первым.
    """
    reply_a = "2026-07-13T17:53:09.100Z"
    reply_b = "2026-07-13T17:53:10.100Z"
    _stand(
        engine, monkeypatch,
        [api_comment("c1", "A", CREATED, replies=[
            {"id": "r1", "createdTime": reply_a,
             "author": {"displayName": "A"}, "content": "ответ"}]),
         api_comment("c2", "A", CREATED, replies=[
             {"id": "r2", "createdTime": reply_b,
              "author": {"displayName": "A"}, "content": "ответ"}])],
        [("0", "A", CREATED_SEC), ("0r", "A", "2026-07-13T17:53:09Z"),
         ("1", "A", CREATED_SEC), ("1r", "A", "2026-07-13T17:53:10Z")],
        [("Alpha", [("0", 0, 5), ("0r", 0, 5)]),
         ("Bravo", [("1", 0, 5), ("1r", 0, 5)]),
         ("Charlie", [])])
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Charlie", "with": "Xray"}]),
        encoding="utf-8")
    engine.patch_doc("doc1", str(ops))
    out = json.loads(capsys.readouterr().out)
    assert out["ops_applied"] == 1
    assert not out.get("refused")


def test_replying_to_only_one_of_the_two_does_not_unblock(engine, monkeypatch,
                                                          tmp_path, capsys):
    """Обратная половина той же пары. Ответ в ОДИН тред делает уникальным
    только его: у второго все ключи по-прежнему общие с первым, и документ
    остаётся запертым.

    Без этого теста предыдущий ничего не доказывал бы — он проходил бы и с
    неверным советом «ответьте в один из них»."""
    _stand(
        engine, monkeypatch,
        [api_comment("c1", "A", CREATED, replies=[
            {"id": "r1", "createdTime": "2026-07-13T17:53:09.100Z",
             "author": {"displayName": "A"}, "content": "ответ"}]),
         api_comment("c2", "A", CREATED)],
        [("0", "A", CREATED_SEC), ("0r", "A", "2026-07-13T17:53:09Z"),
         ("1", "A", CREATED_SEC)],
        [("Alpha", [("0", 0, 5), ("0r", 0, 5)]),
         ("Bravo", [("1", 0, 5)]),
         ("Charlie", [])])
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Charlie", "with": "Xray"}]),
        encoding="utf-8")
    with pytest.raises(SystemExit):
        engine.patch_doc("doc1", str(ops))
    out = json.loads(capsys.readouterr().out)
    assert out["refused"][0]["reason"] == "anchor_identity_collision"


def test_the_code_does_not_depend_on_the_document_having_comments(engine,
                                                                  monkeypatch,
                                                                  tmp_path,
                                                                  capsys):
    """Тот же отказ на ЧИСТОМ документе обязан нести тот же код.

    Первая редакция #51 заполняла только путь комментированного документа, и
    машинный контракт молча зависел от формы файла (codex, ревью r19)."""
    doc = make_doc(["Alpha", "Bravo", "Charlie"])
    docs = DocsStub(doc)
    drive = DriveStub([], _docx_builder(
        docs, [("Alpha", []), ("Bravo", []), ("Charlie", [])], []))
    wire(engine, monkeypatch, docs, drive)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "нет такого", "with": "X"},
        {"op": "replace_quote", "quote": "Charlie", "with": "Xray"},
    ]), encoding="utf-8")
    with pytest.raises(SystemExit):
        engine.patch_doc("doc1", str(ops))
    out = json.loads(capsys.readouterr().out)
    assert out["strategy"] == "index-atomic"      # именно чистый путь
    assert out["refused"][0]["reason"] == "quote_not_found"
    assert out["refused"][0]["details"]["quote"] == "нет такого"


def test_an_early_refusal_on_a_commented_document_keeps_its_code(engine,
                                                                 monkeypatch,
                                                                 tmp_path,
                                                                 capsys):
    """Третий путь квитанции: отказ, вынесенный ДО цикла записи на документе
    С комментариями. Он собирается в другом месте, чем два предыдущих, и
    первая редакция #51 диагноз там теряла (codex, ревью r19)."""
    doc = make_doc(["Alpha", "Bravo", "Charlie"])
    # непринятое предложение ровно на «Charlie»: правка по нему отклоняется
    # предварительной проверкой, не доходя до записи
    doc["body"]["content"][2]["paragraph"]["elements"][0]["textRun"][
        "suggestedInsertionIds"] = ["sug1"]
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []), ("Bravo", [("0", 0, 5)]),
                             ("Charlie", [])], [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Charlie", "with": "Xray"},
        {"op": "replace_quote", "quote": "Alpha", "with": "Yankee"},
    ]), encoding="utf-8")
    with pytest.raises(SystemExit):
        engine.patch_doc("doc1", str(ops))
    out = json.loads(capsys.readouterr().out)
    refused = [r for r in out["refused"] if r["op"] == 0][0]
    assert refused["reason"] == "suggestion_overlap"
    assert refused["details"]["label"]
