"""`patch --output` на пути ЗАПИСИ (r19/T14).

`agents/CONTRACT.md` §2.6 запрещает агенту действовать по обрезанному выводу
и велит брать `--output`. У записи флага не было вовсе — команда отказывала, —
а именно её квитанция несёт `anchor_effects`: что стало с текстом под каждым
комментарием. Переполучить её нельзя, запись уже произошла. Контракт требовал
того, чего команда не умела; живой прогон навыка чужим исполнителем на это и
наткнулся.

Три условия, без которых флаг был бы хуже отказа:

* путь проверяется ДО первого обращения к Google — узнать о негодном пути
  после записи в документ значит потерять единственную квитанцию;
* частичный исход попадает в файл так же, как полный, иначе на коде
  возврата 3 агент останется ровно без того, ради чего флаг заведён;
* сбой записи файла говорит вслух, что документ УЖЕ изменён, — иначе агент
  прочитает ошибку как «ничего не произошло» и повторит правку.
"""

import json

import pytest


def _doc(text="Alpha Beta"):
    return {"revisionId": "R0", "body": {"content": [{
        "startIndex": 1, "endIndex": 1 + len(text),
        "paragraph": {"elements": [{
            "startIndex": 1, "endIndex": 1 + len(text),
            "textRun": {"content": text},
        }]},
    }]}}


class _Docs:
    """Docs-сервис, который считает и чтения, и записи."""

    def __init__(self, doc):
        self._doc = doc
        self.writes = 0
        self.reads = 0

    def documents(self):
        return self

    def get(self, **_):
        self.reads += 1
        doc = self._doc
        return type("R", (), {"execute": lambda self: doc})()

    def batchUpdate(self, **_):
        self.writes += 1
        return type("R", (), {"execute": lambda self: {}})()


def _wire(engine, monkeypatch, doc=None):
    service = _Docs(doc or _doc())
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: service)
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    # Документ без заякоренных комментариев: путь index-atomic, писателю
    # выгрузка не нужна.
    monkeypatch.setattr(engine, "_census_comments", lambda *_: ([], [], "fp", {}))
    return service


def _ops(tmp_path, ops):
    path = tmp_path / "ops.json"
    path.write_text(json.dumps(ops))
    return str(path)


def test_full_receipt_goes_to_the_file_and_a_short_one_to_stdout(
        engine, monkeypatch, tmp_path, capsys):
    service = _wire(engine, monkeypatch)
    target = tmp_path / "receipt.json"
    engine.patch_doc("d1", _ops(tmp_path, [
        {"op": "replace_quote", "quote": "Alpha", "with": "Gamma"}]),
        output=str(target))
    assert service.writes == 1
    short = json.loads(capsys.readouterr().out)
    full = json.loads(target.read_text())
    assert short["written"] == str(target)
    # Короткая квитанция называет исход, иначе с `--output` видно только
    # число записанных байт.
    assert short["action"] == "patched" and short["ops_applied"] == 1
    assert full["action"] == "patched" and full["ops_applied"] == 1
    assert full["doc_id"] == "d1"


def test_partial_result_reaches_the_file_too(engine, monkeypatch, tmp_path,
                                             capsys):
    _wire(engine, monkeypatch)
    target = tmp_path / "receipt.json"
    with pytest.raises(SystemExit) as exc:
        engine.patch_doc("d1", _ops(tmp_path, [
            {"op": "replace_quote", "quote": "Alpha", "with": "Gamma"},
            {"op": "replace_quote", "quote": "Такого нет", "with": "X"}]),
            output=str(target))
    assert exc.value.code == 3
    capsys.readouterr()
    full = json.loads(target.read_text())
    assert full["action"] == "partially-patched"
    assert full["refused"][0]["reason"] == "quote_not_found"


def test_a_bad_path_refuses_before_google_is_touched(engine, monkeypatch,
                                                     tmp_path, capsys):
    service = _wire(engine, monkeypatch)
    link = tmp_path / "ссылка"
    link.symlink_to(tmp_path / "нет-такого-каталога")
    with pytest.raises(SystemExit):
        engine.patch_doc("d1", _ops(tmp_path, [
            {"op": "replace_quote", "quote": "Alpha", "with": "Gamma"}]),
            output=str(link / "receipt.json"))
    out = json.loads(capsys.readouterr().out)
    assert out["reason"] == "output_path_refused"
    # Главное утверждение файла: до Google дело не дошло вовсе.
    assert service.reads == 0 and service.writes == 0


def test_dry_run_output_still_works(engine, monkeypatch, tmp_path, capsys):
    """Флаг не отобрали у холостого прогона, ради которого он заводился."""
    service = _wire(engine, monkeypatch)
    target = tmp_path / "plan.json"
    engine.patch_doc("d1", _ops(tmp_path, [
        {"op": "replace_quote", "quote": "Alpha", "with": "Gamma"}]),
        dry_run=True, output=str(target))
    capsys.readouterr()
    assert service.writes == 0
    assert json.loads(target.read_text())["action"] == "dry-run"


# ---------------------------------------------------------------------------
# Найдено ревью швов (T15)
# ---------------------------------------------------------------------------

def test_the_leaf_is_checked_too_not_only_its_directory(engine, monkeypatch,
                                                        tmp_path, capsys):
    """Каталог годен, а сам файл — симлинк. Проверка, которая смотрит только
    на каталог, пропускает такой путь: документ меняется, и лишь ПОТОМ
    выясняется, что квитанцию писать некуда. Именно её и нельзя переполучить.
    """
    service = _wire(engine, monkeypatch)
    victim = tmp_path / "victim"
    victim.write_text("SECRET")
    target = tmp_path / "receipt.json"
    target.symlink_to(victim)
    with pytest.raises(SystemExit):
        engine.patch_doc("d1", _ops(tmp_path, [
            {"op": "replace_quote", "quote": "Alpha", "with": "Gamma"}]),
            output=str(target))
    assert json.loads(capsys.readouterr().out)["reason"] == "output_path_refused"
    assert service.reads == 0 and service.writes == 0
    assert victim.read_text() == "SECRET"


def test_a_directory_under_the_output_name_is_refused(engine, monkeypatch,
                                                      tmp_path, capsys):
    service = _wire(engine, monkeypatch)
    (tmp_path / "receipt.json").mkdir()
    with pytest.raises(SystemExit):
        engine.patch_doc("d1", _ops(tmp_path, [
            {"op": "replace_quote", "quote": "Alpha", "with": "Gamma"}]),
            output=str(tmp_path / "receipt.json"))
    assert "каталог" in json.loads(capsys.readouterr().out)["error"]
    assert service.reads == 0


def test_an_ordinary_missing_file_is_still_fine(engine, monkeypatch, tmp_path):
    """Ограда узкая: обычный несуществующий путь — нормальный случай, и
    отказывать на нём значило бы отобрать флаг у всех."""
    assert engine._output_problem(str(tmp_path / "нет-такого.json")) is None
    # …и перезапись СВОЕГО прежнего файла тоже нормальна
    (tmp_path / "старый.json").write_text("{}")
    assert engine._output_problem(str(tmp_path / "старый.json")) is None
