"""Защита разрушительного `update` (r19/T12).

`update` — единственная команда, которая уничтожает треды, и отката для неё не
существует. Поэтому здесь проверяется не «работает ли она», а держатся ли три
условия, каждое из которых закрывает свою беду: режим назван человеком, база
доказывает заменяемое состояние, архив снят целиком до первого разрушения.
"""

import json

import pytest


class _Boom(Exception):
    """Запись, до которой дойти было нельзя."""


def _resp(payload):
    return type("R", (), {"execute": lambda self: payload})()


class FakeDrive:
    """Drive, который считает каждый вызов и умеет ломаться где попросят."""

    def __init__(self, *, export=b"PK-docx", explode_on=(), copy_ok=True):
        self.calls = []
        self._export = export
        self._explode_on = set(explode_on)
        self._copy_ok = copy_ok

    def files(self):
        return self

    def get(self, **_):
        self.calls.append("get")
        return _resp({"id": "doc1", "name": "doc", "parents": ["folder1"],
                      "webViewLink": "https://example/doc1"})

    def export(self, **_):
        self.calls.append("export")
        if "export" in self._explode_on:
            raise _Boom("export")
        return _resp(self._export)

    def copy(self, **_):
        self.calls.append("copy")
        if not self._copy_ok:
            raise _Boom("copy")
        return _resp({"id": "copy1", "name": "doc.before-replace-x",
                      "parents": ["folder1"]})

    def update(self, **_):
        self.calls.append("update")
        if "update" in self._explode_on:
            raise _Boom("transport died mid-upload")
        return _resp({})

    def create(self, **_):
        self.calls.append("create")
        return _resp({"id": "new1", "name": "doc (new version)",
                      "webViewLink": "https://example/new1"})


def _wire(monkeypatch, engine, drive, *, comments=(), revisions=("R0",),
          named_ranges=()):
    """Подставить чтения. `revisions` выдаётся по очереди, потом повторяется."""
    seq = list(revisions)
    state = {"i": 0}

    def _doc(*_a, **_k):
        rev = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return {"revisionId": rev,
                "namedRanges": {n: {} for n in named_ranges},
                "body": {"content": []}}

    live = list(comments)
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service", lambda c: drive)
    monkeypatch.setattr(engine, "get_docs_service", lambda c: object())
    monkeypatch.setattr(engine, "_safe_get_doc", _doc)
    monkeypatch.setattr(engine, "_list_comments_raw", lambda d, f: list(live))
    monkeypatch.setattr(engine, "_census_comments",
                        lambda d, f: (list(live), list(live), "fp", {}))
    monkeypatch.setattr(engine, "post_process_images",
                        lambda *a, **k: None)
    monkeypatch.setattr(engine, "post_process_highlights",
                        lambda *a, **k: None)
    return live


def _md(tmp_path, text="# hi\n"):
    p = tmp_path / "doc.md"
    p.write_text(text, encoding="utf-8")
    return str(p)


def _base(tmp_path, engine, *, doc_id="doc1", revision="R0", elements=None):
    p = tmp_path / "doc.md.skrepka-base.json"
    p.write_text(json.dumps({
        "schema_version": engine.SIDECAR_SCHEMA_VERSION,
        "doc_id": doc_id, "revision_id": revision,
        "elements": elements if elements is not None else [],
    }), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# Режим называет человек
# ---------------------------------------------------------------------------

def test_no_mode_writes_nothing_and_names_both_paths(engine, monkeypatch,
                                                     tmp_path, capsys):
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive, comments=[{"id": "c1"}])
    with pytest.raises(SystemExit) as exc:
        engine.update_doc("doc1", _md(tmp_path))
    assert exc.value.code == 2
    assert "update" not in drive.calls and "copy" not in drive.calls
    out = json.loads(capsys.readouterr().out)
    assert "--create-new" in out["create_new"]
    assert "--replace-existing" in out["replace"]
    # Совет, поставленный живым инцидентом #24: назвать не только цену, но и
    # путь, на котором треды остаются живы.
    assert "`patch`" in out["reason"]
    assert "download" in out["reason"] and "sync" in out["reason"]
    assert out["reason"].index("ask the person") < out["reason"].index(
        "--acknowledge-loss")


def test_both_modes_at_once_is_refused(engine, monkeypatch, tmp_path, capsys):
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive)
    with pytest.raises(SystemExit):
        engine.update_doc("doc1", _md(tmp_path), create_new=True,
                          replace_existing=True)
    assert drive.calls == []


@pytest.mark.parametrize("kwargs", [
    {"replace_existing": True},
    {"replace_existing": True, "base": "X"},
    {"replace_existing": True, "acknowledge_loss": True},
])
def test_each_pair_of_flags_is_not_enough(engine, monkeypatch, tmp_path,
                                          capsys, kwargs):
    # Мутант, принимающий любую пару, обязан умереть на каждой из трёх.
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive, comments=[{"id": "c1"}])
    if kwargs.get("base") == "X":
        kwargs["base"] = _base(tmp_path, engine)
    with pytest.raises(SystemExit):
        engine.update_doc("doc1", _md(tmp_path), **kwargs)
    assert "update" not in drive.calls
    capsys.readouterr()


def test_create_new_leaves_the_original_alone(engine, monkeypatch, tmp_path,
                                              capsys):
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive, comments=[{"id": "c1"}])
    engine.update_doc("doc1", _md(tmp_path), create_new=True)
    out = json.loads(capsys.readouterr().out)
    assert "update" not in drive.calls
    # Действие названо ИНАЧЕ: агент, разбирающий квитанцию по полю `action`,
    # обязан увидеть, что документ по прежнему адресу не менялся.
    assert out["action"] == "created-new"
    assert out["original_untouched"] is True
    assert out["original_id"] == "doc1" and out["id"] == "new1"
    assert "doc1" in out["original_url"]


# ---------------------------------------------------------------------------
# База доказывает заменяемое состояние
# ---------------------------------------------------------------------------

def _replace(engine, tmp_path, base, drive):
    return engine.update_doc("doc1", _md(tmp_path), replace_existing=True,
                             base=base, acknowledge_loss=True)


def test_stale_base_refuses_before_any_write(engine, monkeypatch, tmp_path,
                                             capsys):
    # Сайдкар снят на R0, документ живёт на R1: его правили после скачивания,
    # и замена стёрла бы эти правки.
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive, comments=[{"id": "c1"}],
          revisions=("R1",))
    with pytest.raises(SystemExit):
        _replace(engine, tmp_path, _base(tmp_path, engine, revision="R0"),
                 drive)
    assert "update" not in drive.calls and "copy" not in drive.calls
    out = json.loads(capsys.readouterr().out)
    assert out["reason"] == "concurrent_edit"
    assert out["details"]["base_revision"] == "R0"
    assert out["details"]["live_revision"] == "R1"


def test_base_from_another_document_is_refused(engine, monkeypatch, tmp_path,
                                               capsys):
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive)
    with pytest.raises(SystemExit):
        _replace(engine, tmp_path, _base(tmp_path, engine, doc_id="other"),
                 drive)
    assert "update" not in drive.calls
    assert "belongs to doc other" in json.loads(
        capsys.readouterr().out)["error"]


def test_base_of_an_unsupported_schema_is_refused(engine, monkeypatch,
                                                  tmp_path, capsys):
    # Сайдкар старой схемы описывает документ не теми полями, и сверять по
    # нему нечего. Молча принять его значит разрушать по базе, смысл которой
    # изменился.
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive)
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"schema_version": 0, "doc_id": "doc1",
                             "revision_id": "R0", "elements": []}),
                 encoding="utf-8")
    with pytest.raises(SystemExit):
        _replace(engine, tmp_path, str(p), drive)
    assert "update" not in drive.calls
    assert "base schema" in json.loads(capsys.readouterr().out)["error"]


def test_missing_revision_refuses_rather_than_guessing(engine, monkeypatch,
                                                       tmp_path, capsys):
    # Google не отдаёт ревизию документу, на который у аккаунта нет права
    # правки. Доказать базу нечем — разрушать вслепую нельзя.
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive, revisions=(None,))
    with pytest.raises(SystemExit):
        _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    assert "update" not in drive.calls
    # Именно эта причина, а не «база не сошлась»: человеку надо понять, что
    # дело в правах на документ, а не в его собственной базе.
    assert "no revision id" in json.loads(capsys.readouterr().out)["error"]


def test_matching_revision_lets_the_replace_through(engine, monkeypatch,
                                                    tmp_path, capsys):
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive, comments=[{"id": "c1"}])
    _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "replaced"
    assert drive.calls.count("update") == 1


# ---------------------------------------------------------------------------
# Архив снимается целиком и до разрушения
# ---------------------------------------------------------------------------

def test_archive_holds_every_comment_including_deleted(engine, monkeypatch,
                                                       tmp_path, capsys):
    # Копия Drive не переносит ни одного треда (замер 27.08, 62 → 0), поэтому
    # разговоры живут только здесь. И удалённые в том числе: первый элемент
    # `_census_comments` их отфильтровывает, и собрать им «все» нельзя.
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive,
          comments=[{"id": "c1", "content": "живой"},
                    {"id": "c2", "content": "удалённый", "deleted": True}])
    _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    out = json.loads(capsys.readouterr().out)
    archived = json.loads(
        open(out["archive"]["dir"] + "/comments.json").read())
    assert [c["id"] for c in archived] == ["c1", "c2"]
    assert out["archive"]["comments_archived"] == 2
    assert out["copy"]["holds_comments"] is False


def test_archive_manifest_names_a_digest_for_every_file(engine, monkeypatch,
                                                        tmp_path, capsys):
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive)
    _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    out = json.loads(capsys.readouterr().out)
    manifest = json.loads(open(out["archive"]["manifest"]).read())
    names = sorted(f["name"] for f in manifest["files"])
    assert names == ["comments.json", "document.docx", "document.json"]
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])
    assert manifest["revision_id"] == "R0"
    # Названо архивом, а не резервной копией: восстановления по прежнему
    # адресу не существует, и обещать его нельзя.
    assert "not a backup" in manifest["note"]
    # Замерено живой приёмкой 05.09: загрузка docx из архива возвращает не
    # только текст, но и ОТКРЫТЫЕ треды живыми якорными комментариями.
    # Закрытые не возвращаются — их выгрузка не несёт вовсе (M13). Раньше
    # квитанция занижала архив, обещая комментарии «только текстом в JSON».
    assert "OPEN thread" in manifest["note"]
    assert "Closed threads do not" in manifest["note"]


def test_document_edited_during_the_archive_stops_everything(
        engine, monkeypatch, tmp_path, capsys):
    # Ревизия меняется между границами: архив смешал бы два состояния.
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive,
          revisions=("R0", "R0", "R1", "R1", "R2", "R3", "R4", "R5"))
    with pytest.raises(SystemExit):
        _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    assert "update" not in drive.calls
    capsys.readouterr()


def test_comments_edited_during_the_archive_stop_everything(
        engine, monkeypatch, tmp_path, capsys):
    # Ревизия НЕ меняется — правка текста ответа её не двигает, комментарии
    # живут отдельным ресурсом Drive. Ловится только своей границей.
    drive = FakeDrive()
    live = _wire(monkeypatch, engine, drive,
                 comments=[{"id": "c1", "content": "было"}])
    seen = {"n": 0}

    def _raw(_d, _f):
        seen["n"] += 1
        return (list(live) if seen["n"] % 2
                else [{"id": "c1", "content": "стало"}])
    monkeypatch.setattr(engine, "_list_comments_raw", _raw)
    with pytest.raises(SystemExit):
        _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    assert "update" not in drive.calls
    # Проверяется ИМЕННО граница архива, а не предзаписная свежесть: отказ
    # обязан прийти оттуда, где состояние не сошлось, иначе снятие этой
    # ограды прошло бы незамеченным — предзаписная поймала бы расхождение и
    # без неё, только уже с записанным на диск смешанным архивом.
    error = json.loads(capsys.readouterr().out)["error"]
    assert "archive not taken" in error and "comments changed" in error


def test_a_failed_archive_artifact_stops_the_replace(engine, monkeypatch,
                                                     tmp_path, capsys):
    drive = FakeDrive(explode_on=("export",))
    _wire(monkeypatch, engine, drive)
    with pytest.raises(SystemExit):
        _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    assert "update" not in drive.calls
    assert "archive not taken" in json.loads(
        capsys.readouterr().out)["error"]


def test_a_comment_appearing_after_the_archive_stops_the_replace(
        engine, monkeypatch, tmp_path, capsys):
    # Между решением человека и записью в документе появился новый тред. Он
    # исчез бы, а человек так и не узнал бы, что он был.
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive, comments=[{"id": "c1"}])
    calls = {"n": 0}
    real = engine._list_comments_raw

    def _raw(d, f):
        calls["n"] += 1
        if calls["n"] > 2:                 # обе границы архива уже сошлись
            return [{"id": "c1"}, {"id": "c2", "content": "новый"}]
        return real(d, f)
    monkeypatch.setattr(engine, "_list_comments_raw", _raw)
    with pytest.raises(SystemExit):
        _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    assert "update" not in drive.calls
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Исходов записи три, а не два
# ---------------------------------------------------------------------------

def test_transport_failure_gives_outcome_unknown_not_silence(
        engine, monkeypatch, tmp_path, capsys):
    # Обрыв связи после того, как байты легли, выглядит отсюда так же, как
    # обрыв до. Что в документе — неизвестно, и промолчать об этом нельзя.
    drive = FakeDrive(explode_on=("update",))
    _wire(monkeypatch, engine, drive, comments=[{"id": "c1"}])
    with pytest.raises(SystemExit) as exc:
        _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    assert exc.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "outcome-unknown"
    assert out["archive"]["manifest"]          # где искать прежнее состояние


def test_post_processing_failure_is_a_partial_outcome(engine, monkeypatch,
                                                      tmp_path, capsys):
    # Оба постпроцессора внутри превращают отказ в предупреждение, и квитанция
    # поверх них показывала полный успех: документ заменён, картинка осталась
    # текстовым маркером, а `action` говорит «заменено».
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive)
    monkeypatch.setattr(engine, "post_process_highlights",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("свежая ревизия отклонила батч")))
    with pytest.raises(SystemExit) as exc:
        _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    assert exc.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "replaced-partially"
    assert out["post_processing_undone"][0]["step"] == "highlights"


def test_receipt_never_claims_the_write_was_pinned(engine, monkeypatch,
                                                   tmp_path, capsys):
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive, comments=[{"id": "c1"}])
    _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    out = json.loads(capsys.readouterr().out)
    # У media-загрузки Drive предусловия нет вовсе — проверено по
    # дискавери-документу. Обещать закрепление за ревизией нельзя.
    assert "cannot be pinned to a revision" in out["race_window"]
    assert "WHILE the upload was running" in out["race_window"]
    assert "no programmatic rollback" in out["no_rollback"]
    assert "one-time consent" in out["consent_note"]
    assert out["comments_lost"] == 1


def test_document_edited_between_archive_and_write_stops_the_replace(
        engine, monkeypatch, tmp_path, capsys):
    # Обе границы архива сошлись, а перед самой записью документ уже другой.
    # Без предзаписной проверки замена затёрла бы правку, которой архив не
    # видел, и человек не узнал бы, что она была.
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive, revisions=("R0", "R0", "R0", "R1"))
    with pytest.raises(SystemExit):
        _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    assert "update" not in drive.calls
    assert "while the archive was being taken" in json.loads(
        capsys.readouterr().out)["error"]


def test_document_moving_between_base_check_and_archive_stops_it(
        engine, monkeypatch, tmp_path, capsys):
    # Цепочку рвёт середина: базу проверили на R0, документ уехал на R1,
    # архив снялся с R1 и сам с собой сошёлся, предзаписная проверка сошлась
    # с архивом — и замена ушла бы поверх правок, которых человек не видел.
    # Каждое звено сверяется с БАЗОЙ, а не с соседним (найдено ревью кода).
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive, revisions=("R0", "R1"))
    with pytest.raises(SystemExit):
        _replace(engine, tmp_path, _base(tmp_path, engine, revision="R0"),
                 drive)
    assert "update" not in drive.calls
    out = json.loads(capsys.readouterr().out)
    assert out["details"]["base_revision"] == "R0"
    assert out["details"]["archive_revision"] == "R1"


def test_nothing_is_created_when_the_freshness_check_refuses(
        engine, monkeypatch, tmp_path, capsys):
    # Копия Drive — тоже запись, и делать её, когда условие уже не выполнено,
    # значит сорить в чужой папке. Она идёт ПОСЛЕ всех проверок свежести.
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive, revisions=("R0", "R0", "R0", "R1"))
    with pytest.raises(SystemExit):
        _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    assert "copy" not in drive.calls and "update" not in drive.calls
    capsys.readouterr()


def test_a_failing_convenience_copy_does_not_abort_the_replace(
        engine, monkeypatch, tmp_path, capsys):
    # Копия необязательна: архив уже снят целиком. Обрывать из-за неё
    # законную замену значит отказывать там, где всё готово.
    drive = FakeDrive(copy_ok=False)
    _wire(monkeypatch, engine, drive, comments=[{"id": "c1"}])
    _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "replaced"
    assert out["copy"] is None


def test_a_failing_temp_cleanup_never_eats_the_receipt(engine, monkeypatch,
                                                       tmp_path, capsys):
    # Исход уже случился. Промолчать о нём из-за неудалённого временного
    # файла — худшее, что можно сделать после разрушающей записи.
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive)
    # Временный файл появляется только когда в markdown есть картинка: без
    # неё загружается сам исходник и убирать нечего.
    (tmp_path / "pic.png").write_bytes(b"\x89PNG")
    md = _md(tmp_path, "# hi\n\n![alt](pic.png)\n")
    monkeypatch.setattr(engine.os, "unlink",
                        lambda p: (_ for _ in ()).throw(OSError("busy")))
    engine.update_doc("doc1", md, replace_existing=True,
                      base=_base(tmp_path, engine), acknowledge_loss=True)
    assert json.loads(capsys.readouterr().out)["action"] == "replaced"


def test_the_archive_retakes_itself_when_its_two_reads_disagree(
        engine, monkeypatch, tmp_path, capsys):
    # Внутренняя граница архива нужна и после того, как архив сверяется с
    # базой: без неё выгрузка docx попала бы в архив из момента, когда
    # документ был другим, а итоговая ревизия сошлась бы с базой и всё
    # выглядело бы честным. Видно по числу выгрузок: с оградой их две.
    drive = FakeDrive()
    _wire(monkeypatch, engine, drive,
          revisions=("R0", "R1", "R0", "R0", "R0", "R0"))
    _replace(engine, tmp_path, _base(tmp_path, engine), drive)
    assert drive.calls.count("export") == 2
    assert json.loads(capsys.readouterr().out)["action"] == "replaced"
