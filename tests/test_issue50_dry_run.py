"""Холостой прогон `patch` (#50, r19/T11) — враждебный контракт советчика.

Главное утверждение всего файла одно: `would_apply` не должен потом
обернуться предсказуемым отказом или неприменением. Обратной гарантии нет.

Здесь же закреплены три починки писателя, найденные ревью плана T11: полный
перебор пересечений, операция-не-объект и типы полей адреса. Без них советчик
обещал бы применение там, где `patch` падает трейсбеком или молча портит текст.
"""

import json

import pytest


def _tab(text="Alpha"):
    return {"body": {"content": [{
        "startIndex": 1, "endIndex": 1 + len(text),
        "paragraph": {"elements": [{
            "startIndex": 1, "endIndex": 1 + len(text),
            "textRun": {"content": text},
        }]},
    }]}}


def _doc(text="Alpha", **extra):
    out = {"revisionId": "R0", **_tab(text)}
    out.update(extra)
    return out


def _plan(engine, ops, *, anchored=False, text="Alpha", comments=None,
          tab_id=None, doc=None):
    prepared = engine.prepare_patch(
        doc if doc is not None else _doc(text), ops, tab_id=tab_id,
        anchored=anchored,
        comments=comments if comments is not None
        else ([{"id": "c1"}] if anchored else []))
    return engine.compile_index_plan(prepared)


# ---------------------------------------------------------------------------
# Чистый документ: всё решается по снимку
# ---------------------------------------------------------------------------

def test_clean_replace_is_would_apply(engine):
    verdicts = _plan(engine, [{"op": "replace_quote", "quote": "Alpha",
                               "with": "Beta"}])
    assert verdicts[0]["status"] == "would_apply"
    assert "applied" not in verdicts[0]


def test_clean_noop_is_noop(engine):
    assert _plan(engine, [{"op": "replace_quote", "quote": "Alpha",
                           "with": "Alpha"}])[0]["status"] == "noop"


def test_clean_missing_quote_is_would_refuse_with_writer_code(engine):
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Missing",
                              "with": "Beta"}])[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "quote_not_found"


def test_clean_ambiguous_quote_is_would_refuse(engine):
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "a",
                              "with": "b"}], text="banana")[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "quote_ambiguous"


def test_unknown_op_name_is_would_refuse(engine):
    assert _plan(engine, [{"op": "wat", "quote": "Alpha",
                           "with": "Beta"}])[0]["status"] == "would_refuse"


def test_clean_insert_is_would_apply(engine):
    assert _plan(engine, [{"op": "insert_after_quote", "quote": "Alpha",
                           "text": "!"}])[0]["status"] == "would_apply"


def test_clean_empty_insert_is_noop(engine):
    assert _plan(engine, [{"op": "insert_after_quote", "quote": "Alpha",
                           "text": ""}])[0]["status"] == "noop"


def test_suggestion_refusal_keeps_the_writer_reason_code(engine):
    doc = _doc("Alpha")
    (doc["body"]["content"][0]["paragraph"]["elements"][0]["textRun"]
        ["suggestedInsertionIds"]) = ["s1"]
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Alpha",
                              "with": "Beta"}], doc=doc)[0]
    assert verdict["status"] == "would_refuse"
    # Не выдуманный `suggestion_gate`, а тот код, который выдаст сам patch.
    assert verdict["reason"] == "suggestion_overlap"


# ---------------------------------------------------------------------------
# Заякоренный документ: где кончается знание
# ---------------------------------------------------------------------------

def test_commented_destructive_replace_is_unknown(engine):
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Alpha",
                              "with": "Beta"}], anchored=True)[0]
    assert verdict["status"] == "unknown"
    assert verdict["reason"] == "fresh_anchor_map_requires_canary"


def test_commented_pure_insertion_is_would_apply(engine):
    # Замена, которая только удлиняет цель, идёт коротким путём: карта
    # выгрузки для неё не строится вовсе.
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Alpha",
                              "with": "Alpha!"}], anchored=True)[0]
    assert verdict["status"] == "would_apply"


def test_commented_insert_is_would_apply(engine):
    verdict = _plan(engine, [{"op": "insert_after_quote", "quote": "Alpha",
                              "text": "!"}], anchored=True)[0]
    assert verdict["status"] == "would_apply"


def test_commented_anchor_replace_is_unknown(engine):
    verdict = _plan(engine, [{"op": "replace_anchor", "comment_id": "c1",
                              "with": "Beta"}], anchored=True)[0]
    assert verdict["status"] == "unknown"
    assert verdict["reason"] == "fresh_anchor_map_requires_canary"


def test_anchor_operation_without_comments_refuses(engine):
    verdict = _plan(engine, [{"op": "replace_anchor", "comment_id": "c1",
                              "with": "Beta"}])[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "comment_thread_unresolvable"


@pytest.mark.parametrize("census,state", [
    ([{"id": "c1"}, {"id": "c9", "resolved": True}], "resolved"),
    ([{"id": "c1"}, {"id": "c9", "deleted": True}], "deleted"),
    ([{"id": "c1"}], "absent"),
])
def test_thread_that_cannot_be_addressed_is_refused_not_unknown(
        engine, census, state):
    # Опечатка в `comment_id` и закрытый тред — самая частая беда адресации
    # по разговору, и она видна по переписи, без единой записи.
    verdict = _plan(engine, [{"op": "replace_anchor", "comment_id": "c9",
                              "with": "Beta"}],
                    anchored=True, comments=census)[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "comment_thread_unresolvable"
    assert verdict["details"]["thread_state"] == state


# ---------------------------------------------------------------------------
# Статическая проверка T8 и T9: ошибка схемы обязана выглядеть ошибкой схемы
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", [
    {"op": "replace_anchor", "comment_id": "c1", "with": ""},
    {"op": "replace_anchor", "comment_id": "", "with": "x"},
    {"op": "replace_anchor", "comment_id": "c1", "with": "две\nстроки"},
    {"op": "replace_around_anchor", "comment_id": "c1", "quote": "Alpha",
     "with": {"before": "", "after": ""}},
    {"op": "replace_around_anchor", "comment_id": "c1", "quote": "Alpha",
     "with": "не объект"},
    {"op": "replace_around_anchor", "comment_id": "c1", "quote": "",
     "with": {"before": "x", "after": ""}},
])
def test_invalid_late_bound_op_is_static_refusal_not_unknown(engine, op):
    verdict = _plan(engine, [op], anchored=True)[0]
    assert verdict["status"] == "would_refuse", verdict


def test_valid_around_anchor_stays_unknown(engine):
    verdict = _plan(engine, [{"op": "replace_around_anchor",
                              "comment_id": "c1", "quote": "Alpha",
                              "with": {"before": "A", "after": "a"}}],
                    anchored=True)[0]
    assert verdict["status"] == "unknown"


# ---------------------------------------------------------------------------
# Ограды, которые считаются по всему файлу
# ---------------------------------------------------------------------------

def test_two_edits_on_one_thread_refuse_both(engine):
    verdicts = _plan(engine, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "A"},
        {"op": "replace_anchor", "comment_id": "c1", "with": "B"},
    ], anchored=True)
    assert [v["status"] for v in verdicts] == ["would_refuse"] * 2
    assert all(v["reason"] == "unsupported_structure" for v in verdicts)


def test_thread_edit_after_insert_is_refused(engine):
    verdicts = _plan(engine, [
        {"op": "insert_after_quote", "quote": "Alpha", "text": "!"},
        {"op": "replace_anchor", "comment_id": "c1", "with": "Beta"},
    ], anchored=True)
    assert verdicts[0]["status"] == "would_apply"
    assert verdicts[1]["status"] == "would_refuse"
    assert verdicts[1]["reason"] == "concurrent_edit"
    assert verdicts[1]["details"]["after_op"] == 0


def test_thread_edit_before_insert_stays_unknown(engine):
    verdicts = _plan(engine, [
        {"op": "replace_anchor", "comment_id": "c1", "with": "Beta"},
        {"op": "insert_after_quote", "quote": "Alpha", "text": "!"},
    ], anchored=True)
    assert verdicts[0]["status"] == "unknown"


def test_deferred_op_with_explicit_occurrence_is_refused(engine):
    verdicts = _plan(engine, [{"op": "replace_quote", "quote": "Missing",
                               "occurrence": 2, "with": "X"}],
                     anchored=True)
    assert verdicts[0]["status"] == "would_refuse"
    assert verdicts[0]["reason"] == "concurrent_edit"


def test_overlapping_ops_refuse_both(engine):
    verdicts = _plan(engine, [
        {"op": "replace_quote", "quote": "Alpha Beta", "with": "X"},
        {"op": "replace_quote", "quote": "Beta", "with": "Y"},
    ], text="Alpha Beta")
    assert [v["status"] for v in verdicts] == ["would_refuse"] * 2


# ---------------------------------------------------------------------------
# Зависимость по уникальности: только заякоренный путь, и считается композицией
# ---------------------------------------------------------------------------

def test_clean_path_has_no_uniqueness_dependency(engine):
    # Устаревшая семантика `replaceAllText`: он искал текст в момент записи,
    # поэтому ранняя правка могла отнять у поздней уникальность. Индексный
    # писатель резолвит ВЕСЬ файл по одному снимку и шлёт один атомарный
    # батч — зависимости здесь нет и быть не может.
    verdicts = _plan(engine, [
        {"op": "replace_quote", "quote": "Alpha", "with": "B B"},
        {"op": "replace_quote", "quote": "B", "with": "C"},
    ], text="Alpha B")
    assert [v["status"] for v in verdicts] == ["would_apply", "would_apply"]


def test_commented_prior_write_creating_a_copy_is_not_simulated(engine):
    # Ранняя правка только удлиняет цель, значит применится (короткий путь без
    # карты) — и дописывает вторую «B». Поздняя правка по цитате «B» на этом
    # снимке однозначна, а к моменту записи однозначной уже не будет.
    verdicts = _plan(engine, [
        {"op": "replace_quote", "quote": "Alpha", "with": "Alpha B"},
        {"op": "insert_after_quote", "quote": "B", "text": "!"},
    ], text="Alpha zzz B", anchored=True)
    assert verdicts[0]["status"] == "would_apply"
    assert verdicts[1]["status"] == "not_simulated"
    assert verdicts[1]["reason"] == "prior_mutation_may_change_uniqueness"


def test_commented_two_inserts_composing_a_copy_is_not_simulated(engine):
    # Ни одна из вставок сама по себе второй «aXbYc» не создаёт — вместе
    # создают. Подстрочная проверка «вставленный текст содержит цитату» это
    # пропускала, и поздняя правка получала ложный `would_apply`
    # (найдено ревью плана T11).
    verdicts = _plan(engine, [
        {"op": "insert_after_quote", "quote": "a", "occurrence": 2,
         "text": "X"},
        {"op": "insert_after_quote", "quote": "b", "occurrence": 2,
         "text": "Y"},
        {"op": "replace_quote", "quote": "aXbYc", "with": "aXbYc!"},
    ], text="aXbYc abc", anchored=True)
    assert verdicts[0]["status"] == "would_apply"
    assert verdicts[1]["status"] == "would_apply"
    assert verdicts[2]["status"] == "not_simulated"
    assert verdicts[2]["reason"] == "prior_mutation_may_change_uniqueness"
    assert verdicts[2]["depends_on"] == [0, 1]


def test_commented_independent_writes_keep_their_promise(engine):
    # Обратная сторона той же проверки: правка, на уникальность которой
    # соседки не влияют, обещание сохраняет. Без этого `not_simulated` стал бы
    # безусловным и советчик перестал бы что-либо советовать.
    verdicts = _plan(engine, [
        {"op": "insert_after_quote", "quote": "aaa", "text": "!"},
        {"op": "replace_quote", "quote": "ccc", "with": "ccc?"},
    ], text="aaa bbb ccc", anchored=True)
    assert [v["status"] for v in verdicts] == ["would_apply", "would_apply"]


def test_stale_ambiguity_after_an_unknown_is_not_a_refusal(engine):
    # Ранняя правка уносит одну из копий, и поздняя неоднозначность перестаёт
    # быть неоднозначностью. Настаивать на отказе — ложная тревога; каскад
    # снимает и её, а не только обещания, потому что она тоже стоит на тексте.
    verdicts = _plan(engine, [
        {"op": "replace_quote", "quote": "aa bb", "with": "zz"},
        {"op": "replace_quote", "quote": "bb", "with": "cc"},
    ], text="aa bb cc bb", anchored=True)
    assert verdicts[0]["status"] == "unknown"
    assert verdicts[1]["status"] == "not_simulated"


def test_cascade_after_unknown_removes_the_promise(engine):
    verdicts = _plan(engine, [
        {"op": "replace_quote", "quote": "Alpha", "with": "Beta"},
        {"op": "insert_after_quote", "quote": "Gamma", "text": "!"},
    ], anchored=True, text="Alpha Gamma")
    assert verdicts[0]["status"] == "unknown"
    assert verdicts[1]["status"] == "not_simulated"
    assert verdicts[1]["reason"] == "prior_unsimulated_mutation"
    assert verdicts[1]["depends_on"] == [0]


# ---------------------------------------------------------------------------
# Условное намерение ответа (T9/T10)
# ---------------------------------------------------------------------------

def test_around_anchor_declares_a_conditional_reply(engine):
    verdict = _plan(engine, [{"op": "replace_around_anchor",
                              "comment_id": "c1", "quote": "Alpha",
                              "with": {"before": "A", "after": "a"}}],
                    anchored=True)[0]
    assert verdict["would_reply"]["comment_id"] == "c1"
    assert verdict["would_reply"]["state"] == "conditional"
    # Точный текст ответа не обещается: он строится из подтверждённого
    # свежего эффекта, которого у читающего пути нет.
    assert "text" not in verdict["would_reply"]


def test_later_insert_suppresses_the_mandatory_reply(engine):
    verdicts = _plan(engine, [
        {"op": "replace_around_anchor", "comment_id": "c1", "quote": "Alpha",
         "with": {"before": "A", "after": "a"}},
        {"op": "insert_after_quote", "quote": "Gamma", "text": "!"},
    ], anchored=True, text="Alpha Gamma")
    assert verdicts[0]["would_reply"]["suppressed_if_applied"]["by_op"] == 1


def test_plain_replace_declares_no_reply(engine):
    verdict = _plan(engine, [{"op": "replace_anchor", "comment_id": "c1",
                              "with": "Beta"}], anchored=True)[0]
    assert "would_reply" not in verdict


# ---------------------------------------------------------------------------
# Глобальные ограждения писателя
# ---------------------------------------------------------------------------

def test_snapshot_without_revision_refuses_every_write(engine):
    doc = _doc("Alpha")
    doc.pop("revisionId")
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Alpha",
                              "with": "Beta"}], doc=doc)[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "missing_revision_id"


def test_snapshot_without_revision_keeps_a_noop_a_noop(engine):
    # Правка, которая ничего не пишет, ни одного запроса не отправляет, и
    # `_write_control` для неё не вызывается вовсе — отказ здесь был бы
    # выдуманным (найдено ревью плана T11).
    doc = _doc("Alpha")
    doc.pop("revisionId")
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Alpha",
                              "with": "Alpha"}], doc=doc)[0]
    assert verdict["status"] == "noop"


def test_c5_gate_closed_blocks_the_whole_file(engine, monkeypatch):
    monkeypatch.setattr(engine, "C5_INSERT_NEAR_ANCHOR_SAFE", None)
    verdicts = _plan(engine, [
        {"op": "insert_after_quote", "quote": "Alpha", "text": "!"},
        {"op": "replace_anchor", "comment_id": "c1", "with": "Beta"},
    ], anchored=True)
    assert [v["status"] for v in verdicts] == ["would_refuse"] * 2
    assert verdicts[0]["reason"] == "insert_near_anchor_unverified"


def test_c5_gate_verified_lets_inserts_through(engine):
    assert engine.C5_INSERT_NEAR_ANCHOR_SAFE is True
    verdict = _plan(engine, [{"op": "insert_after_quote", "quote": "Alpha",
                              "text": "!"}], anchored=True)[0]
    assert verdict["status"] == "would_apply"


# ---------------------------------------------------------------------------
# Выбор вкладки
# ---------------------------------------------------------------------------

def _multi_tab(*tabs):
    return {"revisionId": "R0", "tabs": [
        {"tabProperties": {"tabId": tid, "title": title},
         "documentTab": _tab(text)}
        for tid, title, text in tabs]}


def test_multi_tab_without_selection_is_read_only_refusal(engine):
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Alpha",
                              "with": "Beta"}],
                    doc=_multi_tab(("t1", "one", "Alpha"),
                                   ("t2", "two", "Alpha")))[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "tab_selection"


def test_explicit_tab_is_decidable(engine):
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Beta",
                              "with": "Gamma"}], tab_id="t2",
                    doc=_multi_tab(("t1", "one", "Alpha"),
                                   ("t2", "two", "Beta")))[0]
    assert verdict["status"] == "would_apply"


def test_duplicate_tab_id_refuses_with_bounded_identity(engine):
    doc = _multi_tab(("dup", "first", "Alpha"), ("dup", "second", "Beta"))
    tid, selected, error = engine._dry_run_select_tab(doc, tab_id="dup")
    assert tid is None and selected is None
    assert error["details"]["code"] == "duplicate_tab_id"
    assert [c["title"] for c in error["details"]["candidates"]] == [
        "first", "second"]
    verdict = _plan(engine, [{"op": "replace_quote", "quote": "Alpha",
                              "with": "X"}], tab_id="dup", doc=doc)[0]
    assert verdict["status"] == "would_refuse"


def test_missing_tab_id_keeps_not_found_semantics(engine):
    _tid, _sel, error = engine._dry_run_select_tab(
        _multi_tab(("t1", "one", "Alpha")), tab_id="missing")
    assert error["details"]["code"] == "tab_not_found"


# ---------------------------------------------------------------------------
# Словарь статусов и причин — публичный контракт
# ---------------------------------------------------------------------------

def test_verdict_builder_rejects_an_unknown_status(engine):
    with pytest.raises(ValueError):
        engine._dry_verdict(0, "src", "probably_fine")


def test_verdict_builder_rejects_an_unknown_reason(engine):
    with pytest.raises(ValueError):
        engine._dry_verdict(0, "src", "would_refuse", reason="safety_gate")


def test_verdict_builder_accepts_both_vocabularies(engine):
    assert engine._dry_verdict(0, "s", "would_refuse",
                               reason="quote_not_found")["reason"]
    assert engine._dry_verdict(0, "s", "unknown",
                               reason="fresh_anchor_map_requires_canary")


# ---------------------------------------------------------------------------
# Вход целиком: ни одной записи, коды возврата, квитанция
# ---------------------------------------------------------------------------

class _ReadOnlyDocs:
    """Docs-сервис, у которого запись — провал теста."""

    def __init__(self, doc):
        self._doc = doc
        self.writes = 0

    def documents(self):
        return self

    def get(self, **_):
        doc = self._doc
        return type("R", (), {"execute": lambda self: doc})()

    def batchUpdate(self, **_):
        self.writes += 1
        raise AssertionError("холостой прогон вызвал batchUpdate")


def _wire(engine, monkeypatch, doc, anchored=()):
    service = _ReadOnlyDocs(doc)
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: service)
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments",
                        lambda *_: (list(anchored), list(anchored), "fp", {}))
    return service


def _ops_file(tmp_path, ops):
    path = tmp_path / "ops.json"
    path.write_text(json.dumps(ops))
    return str(path)


def test_entrypoint_reads_only_and_reports_zero_writes(
        engine, monkeypatch, tmp_path, capsys):
    service = _wire(engine, monkeypatch, _doc())
    engine.dry_run_patch("d1", _ops_file(tmp_path, [
        {"op": "replace_quote", "quote": "Alpha", "with": "Beta"}]))
    out = json.loads(capsys.readouterr().out)
    assert service.writes == 0
    assert out["action"] == "dry-run" and out["writes_performed"] == 0
    assert out["doc_strategy"] == "index-atomic"
    assert out["revision_id_before"] == "R0"
    assert out["operations"][0]["status"] == "would_apply"


def test_entrypoint_names_the_anchor_safe_strategy(
        engine, monkeypatch, tmp_path, capsys):
    _wire(engine, monkeypatch, _doc(), anchored=[{"id": "c1"}])
    with pytest.raises(SystemExit) as exc:
        engine.dry_run_patch("d1", _ops_file(tmp_path, [
            {"op": "replace_quote", "quote": "Alpha", "with": "Beta"}]))
    assert exc.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["doc_strategy"] == "anchor-safe-per-op"
    assert out["operations"][0]["status"] == "unknown"


def test_entrypoint_exit_zero_only_when_everything_would_apply(
        engine, monkeypatch, tmp_path, capsys):
    _wire(engine, monkeypatch, _doc())
    engine.dry_run_patch("d1", _ops_file(tmp_path, [
        {"op": "replace_quote", "quote": "Alpha", "with": "Beta"},
        {"op": "replace_quote", "quote": "Alpha", "with": "Alpha"}]))
    capsys.readouterr()


def test_entrypoint_exit_three_on_refusal(engine, monkeypatch, tmp_path,
                                          capsys):
    _wire(engine, monkeypatch, _doc())
    with pytest.raises(SystemExit) as exc:
        engine.dry_run_patch("d1", _ops_file(tmp_path, [
            {"op": "replace_quote", "quote": "Missing", "with": "Beta"}]))
    assert exc.value.code == 3
    capsys.readouterr()


def test_malformed_ops_file_is_receipted_not_crashed(engine, tmp_path,
                                                     capsys):
    path = tmp_path / "ops.json"
    path.write_text("not-json")
    with pytest.raises(SystemExit) as exc:
        engine.dry_run_patch("d1", str(path))
    assert exc.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "dry-run" and out["writes_performed"] == 0
    assert out["operations"][0]["reason"] == "schema_invalid"


def test_empty_ops_file_is_receipted(engine, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        engine.dry_run_patch("d1", _ops_file(tmp_path, []))
    assert exc.value.code == 3
    assert json.loads(capsys.readouterr().out)["writes_performed"] == 0


def test_output_file_gets_the_full_receipt(engine, monkeypatch, tmp_path,
                                           capsys):
    _wire(engine, monkeypatch, _doc())
    out_path = tmp_path / "receipt.json"
    engine.dry_run_patch("d1", _ops_file(tmp_path, [
        {"op": "replace_quote", "quote": "Alpha", "with": "Beta"}]),
        output=str(out_path))
    assert json.loads(out_path.read_text())["action"] == "dry-run"
    assert json.loads(capsys.readouterr().out)["written"] == str(out_path)


def test_output_without_dry_run_is_refused(engine, tmp_path, capsys):
    with pytest.raises(SystemExit):
        engine.patch_doc("d1", _ops_file(tmp_path, [{"op": "x"}]),
                         output=str(tmp_path / "r.json"))
    assert "--dry-run" in capsys.readouterr().out


@pytest.mark.parametrize("name", [
    "_fresh_anchor_snapshot", "_cleanup_canary", "_apply_op_anchor_safe",
    "_execute_index_replace", "_send_reply_intents", "_fold_reply_intents",
    "_docx_comment_records", "_write_control", "mark_range",
])
def test_writer_export_and_reply_are_unreachable(engine, monkeypatch,
                                                 tmp_path, name):
    # Ни одной пропущенной проверки: имя, которого в движке нет, — это не
    # «нечего проверять», а сломанный сторож, и молчащий сторож хуже
    # отсутствующего.
    assert hasattr(engine, name), name
    monkeypatch.setattr(engine, name, lambda *a, **k: (_ for _ in ()).throw(
        AssertionError(f"холостой прогон вызвал {name}")))
    _wire(engine, monkeypatch, _doc(), anchored=[{"id": "c1"}])
    with pytest.raises(SystemExit):
        engine.dry_run_patch("d1", _ops_file(tmp_path, [
            {"op": "replace_quote", "quote": "Alpha", "with": "Beta"},
            {"op": "replace_around_anchor", "comment_id": "c1",
             "quote": "Alpha", "with": {"before": "A", "after": "a"}}]))


def test_planner_takes_a_snapshot_not_a_service(engine):
    import inspect
    for fn in (engine.prepare_patch, engine.compile_index_plan,
               engine.decide_op):
        names = set(inspect.signature(fn).parameters)
        assert not {"docs_service", "drive_service", "service"} & names, fn


# ---------------------------------------------------------------------------
# Починки писателя, найденные ревью плана T11
# ---------------------------------------------------------------------------

def _ranges(*spans):
    return {i: {"affect_start": s, "affect_end": e, "source": f"r{i}"}
            for i, (s, e) in enumerate(spans)}


def test_overlap_wide_range_conflicts_with_nonadjacent_inner_range(engine):
    # Сравнение только соседей по сортировке пропускало `[4,5]` внутрь
    # `[1,7]`: на чистом пути запросы идут в обратном порядке индексов, и
    # удаление `[1,7]` затирало уже записанное — порча текста молча.
    assert set(engine._ops_overlap_conflicts(
        _ranges((1, 7), (2, 3), (8, 9), (4, 5)))) == {0, 1, 3}


def test_overlap_nested_and_identical_ranges_conflict_both(engine):
    assert set(engine._ops_overlap_conflicts(_ranges((1, 10), (3, 4)))) == {0, 1}
    assert set(engine._ops_overlap_conflicts(_ranges((2, 5), (2, 5)))) == {0, 1}
    assert set(engine._ops_overlap_conflicts(_ranges((1, 4), (4, 4)))) == {0, 1}


def test_non_overlapping_ranges_remain_decidable(engine):
    assert engine._ops_overlap_conflicts(_ranges((1, 3), (4, 4))) == {}


@pytest.mark.parametrize("bad", [
    {"op": "replace_quote", "quote": 3, "with": "X"},
    {"op": "replace_quote", "quote": "Alpha", "with": 3},
    {"op": "replace_quote", "quote": "Alpha", "occurrence": "x", "with": "X"},
    {"op": "replace_quote", "quote": "Alpha", "occurrence": True, "with": "X"},
    {"op": "replace_quote", "quote": "Alpha", "occurrence": 0, "with": "X"},
    {"op": "replace_range", "range": [], "text": "X"},
    {"op": "insert_after_quote", "quote": "Alpha", "text": 3},
])
def test_bad_field_type_is_a_per_op_refusal_not_a_traceback(engine, bad):
    # До починки эти операции давали TypeError/ValueError мимо PatchOpError:
    # процесс падал трейсбеком, и валидные соседки по файлу не применялись.
    engine._RAISE_ERRORS = True
    try:
        with pytest.raises(engine.PatchOpError) as exc:
            engine._resolve_op(bad, _tab(), None)
    finally:
        engine._RAISE_ERRORS = False
    # Код причины проверяется вместе с фактом отказа: `occurrence: 0` и без
    # проверки типа кончится отказом, только назовёт его «цитата не найдена» —
    # то есть отправит человека искать несуществующую опечатку в тексте.
    assert exc.value.reason == "schema_invalid"
    verdict = _plan(engine, [bad])[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "schema_invalid"


@pytest.mark.parametrize("bad", [None, 3, "op", []])
def test_non_object_op_does_not_cost_its_neighbours(engine, monkeypatch,
                                                    capsys, bad):
    calls = {"n": 0}

    class Docs:
        def documents(self):
            return self

        def get(self, **_):
            return type("R", (), {"execute": lambda self: _doc()})()

        def batchUpdate(self, **kw):
            calls["n"] += 1
            calls["body"] = kw["body"]
            return type("R", (), {"execute": lambda self: {}})()

    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: Docs())
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments", lambda *_: ([], [], "f", {}))
    import tempfile
    import os as _os
    fd, path = tempfile.mkstemp(suffix=".json")
    with _os.fdopen(fd, "w") as f:
        json.dump([bad, {"op": "replace_quote", "quote": "Alpha",
                         "with": "Beta"}], f)
    try:
        with pytest.raises(SystemExit) as exc:
            engine.patch_doc("d1", path)
    finally:
        _os.unlink(path)
    # Частичное применение, а не трейсбек: соседка записана.
    assert exc.value.code == 3 and calls["n"] == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ops_applied"] == 1
    assert out["refused"][0]["reason"] == "schema_invalid"


def test_dry_run_agrees_with_the_writer_on_a_non_object_op(engine):
    verdicts = _plan(engine, [None, {"op": "replace_quote", "quote": "Alpha",
                                     "with": "Beta"}])
    assert verdicts[0]["status"] == "would_refuse"
    assert verdicts[0]["reason"] == "schema_invalid"
    assert verdicts[1]["status"] == "would_apply"


def test_insert_at_the_very_end_keeps_the_next_promise(engine):
    # Точка вставки после последней цитаты документа — это «за концом»
    # буфера. Без неё сложение ранних правок не находит места записи и
    # снимает обещание у независимой соседки: советчик становится
    # осторожным там, где знать всё-таки может.
    verdicts = _plan(engine, [
        {"op": "insert_after_quote", "quote": "zzz", "text": "!"},
        {"op": "insert_after_quote", "quote": "abc", "text": "?"},
    ], text="abc zzz", anchored=True)
    assert [v["status"] for v in verdicts] == ["would_apply", "would_apply"]


def test_patch_dry_run_flag_never_reaches_the_writer(engine, monkeypatch,
                                                     tmp_path, capsys):
    # Мерка «холостой» — не в названии функции, а во флаге, которым её
    # зовут. Сорвись здесь ветвление, `patch --dry-run` записал бы документ.
    service = _wire(engine, monkeypatch, _doc(), anchored=[{"id": "c1"}])
    with pytest.raises(SystemExit) as exc:
        engine.patch_doc("d1", _ops_file(tmp_path, [
            {"op": "replace_quote", "quote": "Alpha", "with": "Beta"}]),
            dry_run=True)
    assert exc.value.code == 3 and service.writes == 0
    assert json.loads(capsys.readouterr().out)["action"] == "dry-run"


def test_document_level_comment_is_not_an_anchor_to_address(engine):
    # У документа могут быть комментарии БЕЗ привязки к тексту. Тред в
    # переписи есть и он открыт, а якоря у него нет ни одного, значит
    # адресовать по нему нечего — и писатель это знает по тому же признаку.
    verdict = _plan(engine, [{"op": "replace_anchor", "comment_id": "c1",
                              "with": "Beta"}],
                    anchored=False, comments=[{"id": "c1"}])[0]
    assert verdict["status"] == "would_refuse"
    assert verdict["reason"] == "comment_thread_unresolvable"


def test_unplaceable_prior_write_removes_the_promise(engine, monkeypatch):
    # Сложить ранние правки не вышло — значит про уникальность поздней ничего
    # не известно. Промолчать здесь и оставить обещание значит соврать ровно в
    # ту сторону, которую эта проверка и заводили закрывать.
    monkeypatch.setattr(engine, "_dry_mutated_buffer",
                        lambda *a, **k: None)
    verdicts = _plan(engine, [
        {"op": "insert_after_quote", "quote": "zzz", "text": "!"},
        {"op": "insert_after_quote", "quote": "abc", "text": "?"},
    ], text="abc zzz", anchored=True)
    assert verdicts[0]["status"] == "would_apply"
    assert verdicts[1]["status"] == "not_simulated"


def test_writer_names_a_schema_error_a_schema_error(engine, monkeypatch,
                                                    capsys, tmp_path):
    # Тот же разбор, но у писателя: `occurrence: "два"` не разрешился не
    # потому, что документ изменился под правкой. Отказ, названный
    # `concurrent_edit`, отправляет человека искать чужую правку вместо
    # своей опечатки.
    class Docs:
        def documents(self):
            return self

        def get(self, **_):
            return type("R", (), {"execute": lambda self: _doc()})()

        def batchUpdate(self, **_):
            raise AssertionError("нечего применять")

    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _: Docs())
    monkeypatch.setattr(engine, "get_drive_service", lambda _: object())
    monkeypatch.setattr(engine, "_census_comments",
                        lambda *_: ([], [], "fp", {}))
    with pytest.raises(SystemExit) as exc:
        engine.patch_doc("d1", _ops_file(tmp_path, [
            {"op": "replace_quote", "quote": "Alpha", "occurrence": "два",
             "with": "X"}]))
    assert exc.value.code == 3
    assert json.loads(capsys.readouterr().out)["refused"][0]["reason"] == (
        "schema_invalid")


def test_refusal_that_a_neighbour_can_lift_still_counts_as_a_writer(engine):
    # Найдено ревью кода. Отказ на снимке — не доказательство, что правка не
    # напишет: заякоренный путь применяет операции по одной, перечитывая
    # документ. Здесь первая вставка уносит вторую «ba», после чего вторая
    # операция перестаёт быть неоднозначной, применяется и приносит вторую
    # «Q» — а третья, которой советчик успел пообещать применение, получает
    # `quote_ambiguous`.
    verdicts = _plan(engine, [
        {"op": "insert_after_quote", "quote": "Xb", "text": "!"},
        {"op": "insert_before_quote", "quote": "ba", "text": "Q"},
        {"op": "insert_before_quote", "quote": "Q", "text": "!"},
    ], text="XbaQCba", anchored=True)
    assert verdicts[0]["status"] == "would_apply"
    assert verdicts[1]["status"] == "not_simulated"
    assert verdicts[2]["status"] == "not_simulated"
    assert verdicts[2]["reason"] == "prior_unsimulated_mutation"
    assert verdicts[2]["depends_on"] == [1]


def test_a_final_refusal_does_not_poison_the_rest_of_the_file(engine):
    # Обратная сторона той же поправки. Отказ, которого соседке не снять —
    # число совпадений его цитаты не менялось, — окончателен, и травить им
    # остальной файл значит не советовать ничего.
    verdicts = _plan(engine, [
        {"op": "replace_quote", "quote": "aa", "with": "X"},
        {"op": "insert_after_quote", "quote": "bb", "text": "!"},
    ], text="aa bb aa", anchored=True)
    assert verdicts[0]["status"] == "would_refuse"
    assert verdicts[0]["reason"] == "quote_ambiguous"
    assert verdicts[1]["status"] == "would_apply"


def test_a_schema_refusal_is_never_treated_as_a_possible_writer(engine):
    # Опечатку в схеме соседка по тексту не исправит, и считать такую правку
    # «может быть, напишет» значит терять обещания даром.
    verdicts = _plan(engine, [
        {"op": "wat", "quote": "aa", "with": "X"},
        {"op": "insert_after_quote", "quote": "bb", "text": "!"},
    ], text="aa bb", anchored=True)
    assert verdicts[0]["status"] == "would_refuse"
    assert verdicts[1]["status"] == "would_apply"


def test_schema_refusal_after_a_writer_stays_a_refusal(engine):
    # Та же поправка с другой стороны: соседка меняет число совпадений «zz»,
    # но операция отказала не по цели, а по схеме — снять такой отказ текстом
    # нельзя, и объявлять её «может быть, напишет» значит терять обещания.
    verdicts = _plan(engine, [
        {"op": "insert_after_quote", "quote": "aa", "text": " zz"},
        {"op": "wat", "quote": "zz", "with": "X"},
        {"op": "insert_after_quote", "quote": "bb", "text": "!"},
    ], text="aa bb zz", anchored=True)
    assert verdicts[0]["status"] == "would_apply"
    assert verdicts[1]["status"] == "would_refuse"
    assert verdicts[2]["status"] == "would_apply"


@pytest.mark.parametrize("silent", [
    {"op": "insert_before_quote", "quote": "ba", "text": ""},
    {"op": "replace_quote", "quote": "ba", "with": "ba"},
])
def test_an_op_that_writes_nothing_never_poisons_the_file(engine, silent):
    # Обе формы «ничего не напишу» — вставка без текста и замена текста на
    # него же — записью стать не могут, что бы соседки ни сделали. Свой
    # вердикт им поменять можно (цитата исчезла, писатель откажет вместо
    # тихого «ничего не делаю»), а снимать обещание с соседей — нельзя.
    verdicts = _plan(engine, [
        {"op": "insert_after_quote", "quote": "Xb", "text": "!"},
        silent,
        {"op": "insert_before_quote", "quote": "Q", "text": "!"},
    ], text="XbaQC", anchored=True)
    assert verdicts[0]["status"] == "would_apply"
    assert verdicts[1]["status"] == "not_simulated"
    assert verdicts[2]["status"] == "would_apply"


def test_an_untouched_noop_stays_a_noop(engine):
    verdicts = _plan(engine, [
        {"op": "insert_after_quote", "quote": "Q", "text": "!"},
        {"op": "replace_quote", "quote": "ba", "with": "ba"},
    ], text="XbaQC", anchored=True)
    assert [v["status"] for v in verdicts] == ["would_apply", "noop"]


def test_two_writes_at_one_point_compose_in_operation_order(engine):
    # Порядок наложения при равной позиции решает номер операции, а не текст:
    # писатель применяет их по очереди, и текст ранней оказывается слева.
    # Через `compile_index_plan` такой вход сегодня не проходит — ограда
    # пересечений отклоняет обе правки, — поэтому проверяется сама функция.
    doc_tab = _tab("XWQ")
    buf, imap = engine._text_buffer(doc_tab)
    at = {}
    for j, ch in enumerate(buf):
        d = imap[j]
        if d >= 0:
            at.setdefault(d, j)
            at.setdefault(d + (2 if ord(ch) > 0xFFFF else 1), j + 1)

    def _writer(index, text):
        return {"index": index, "insertion": None,
                "op": {"op": "insert_before_quote", "quote": "W",
                       "text": text},
                "resolved": {"kind": "insert", "start": 2, "end": 2,
                             "text": text}}

    assert engine._dry_mutated_buffer(
        [_writer(0, "a"), _writer(1, "z")], buf, at, doc_tab) == "XazWQ"
    assert engine._dry_mutated_buffer(
        [_writer(0, "z"), _writer(1, "a")], buf, at, doc_tab) == "XzaWQ"
