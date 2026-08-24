"""Пересборка структуры документа: переезд блока как отдельный вердикт.

Замер M21: difflib раскладывает любую перестановку в удаление плюс вставку
с тем же ключом. Раньше переехавший абзац приезжал как свежий блок из
markdown и молча терял оформление, которого markdown не выражает.
"""

import json

import pytest

from test_sync_anchors import (CREATED, CREATED_SEC, DocsStub, DriveStub,
                               _crossing_docx, _docx_builder, api_comment,
                               make_doc, make_workdir, wire)


# ---------------------------------------------------------------------------
# фикстуры
# ---------------------------------------------------------------------------

def make_doc_runs(paras):
    """Документ, где абзац задан списком (текст, textStyle) — по run на пару.

    make_doc из test_sync_anchors кладёт один run без стилей; здесь нужно
    неоднородное оформление внутри абзаца — тот самый случай из замера.
    """
    content, idx = [], 1
    for runs in paras:
        if isinstance(runs, str):
            runs = [(runs, {})]
        text = "".join(t for t, _s in runs)
        s, e = idx, idx + len(text) + 1
        elements, pos = [], s
        for k, (t, style) in enumerate(runs):
            piece = t + ("\n" if k == len(runs) - 1 else "")
            elements.append({"startIndex": pos, "endIndex": pos + len(piece),
                             "textRun": {"content": piece,
                                         "textStyle": dict(style)}})
            pos += len(piece)
        content.append({"startIndex": s, "endIndex": e, "paragraph": {
            "elements": elements,
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}})
        idx = e
    return {"documentId": "doc1", "revisionId": "R0",
            "body": {"content": content}}


def plain(paras):
    """Тексты абзацев из формы make_doc_runs."""
    return ["".join(t for t, _s in p) if not isinstance(p, str) else p
            for p in paras]


def text_batch(docs):
    """Батч, состоящий только из правок текста (не канарейка, не стили)."""
    for reqs in docs.batches:
        kinds = {k for r in reqs for k in r}
        if kinds <= {"insertText", "deleteContentRange"} and len(reqs) > 1:
            return reqs
    for reqs in docs.batches:
        kinds = {k for r in reqs for k in r}
        if kinds <= {"insertText", "deleteContentRange"}:
            return reqs
    raise AssertionError("текстового батча нет")


def style_batch(docs):
    for reqs in docs.batches:
        if any("updateTextStyle" in r or "updateParagraphStyle" in r
               for r in reqs):
            return reqs
    raise AssertionError("стилевого батча нет")


def replay(texts, reqs):
    """Проиграть батч по модели документа и вернуть получившиеся абзацы.

    Индексация как в Docs: тело начинается с 1, каждый абзац кончается
    переводом строки. Запросы применяются по порядку, как их применяет API,
    поэтому проверка ловит именно арифметику индексов.
    """
    body = "".join(t + "\n" for t in texts)
    for r in reqs:
        if "insertText" in r:
            i = r["insertText"]["location"]["index"]
            assert 1 <= i <= len(body) + 1, f"вставка вне тела: {i}"
            body = body[:i - 1] + r["insertText"]["text"] + body[i - 1:]
        elif "deleteContentRange" in r:
            rng = r["deleteContentRange"]["range"]
            s, e = rng["startIndex"], rng["endIndex"]
            assert 1 <= s < e <= len(body) + 1, f"удаление вне тела: {s}..{e}"
            body = body[:s - 1] + body[e - 1:]
        else:
            raise AssertionError(f"в текстовом батче лишний запрос {sorted(r)}")
    out = body.split("\n")
    assert out[-1] == "", "тело перестало кончаться переводом строки"
    return out[:-1]


def run_sync(engine, monkeypatch, tmp_path, base, local, doc=None,
             comments=(), anchors=(), tail_empty=False, sidecar_doc=None,
             merged_texts=None):
    """Прогнать sync с base→local и вернуть стаб Docs.

    sidecar_doc — база, если она должна отличаться от живого документа
    (коллега что-то сделал после скачивания).
    """
    doc = doc or make_doc(base)
    if tail_empty:
        content = doc["body"]["content"]
        end = content[-1]["endIndex"]
        content.append({"startIndex": end, "endIndex": end + 1, "paragraph": {
            "elements": [{"startIndex": end, "endIndex": end + 1,
                          "textRun": {"content": "\n", "textStyle": {}}}],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}})
    merged = make_doc(merged_texts or local, rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    paras = [(t, [("0", 0, len(t))] if t in anchors else []) for t in base]
    centries = [("0", "A", CREATED_SEC)] if anchors else []
    drive = DriveStub(list(comments), _docx_builder(docs, paras, centries),
                      html=("".join(f"<p>{t}</p>"
                                    for t in (merged_texts or local))).encode())
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, sidecar_doc or doc,
                      "\n\n".join(base), "\n\n".join(local))
    engine.sync_doc("doc1", md)
    return docs


A, B, C, D = "Alpha", "Bravo", "Charlie", "Delta"


# ---------------------------------------------------------------------------
# _pair_moved_blocks
# ---------------------------------------------------------------------------

def _p(text, kind="p"):
    return {"type": kind, "text": text}


def test_pair_moved_matches_a_delete_with_the_same_insert(engine):
    base = [_p(A), _p(B), _p(C)]
    local = [_p(B), _p(A), _p(C)]
    pairs = engine._pair_moved_blocks([1], [(0, _p(B))], base, local)
    assert pairs == {("p", B): 1}


def test_pair_moved_needs_the_same_structural_type(engine):
    base, local = [_p(A, "h2")], [_p(A)]
    assert engine._pair_moved_blocks([0], [(1, _p(A))], base, local) == {}


def test_pair_moved_refuses_a_duplicate_inside_the_changed_zone(engine):
    base, local = [_p(B), _p(B)], [_p(B)]
    assert engine._pair_moved_blocks([0, 1], [(0, _p(B))], base, local) == {}


def test_pair_moved_refuses_a_twin_sitting_outside_the_changed_zone(engine):
    """Близнец в нетронутой части документа — всё ещё близнец.

    Считать повторы только по изменённой зоне значит объявить переездом
    абзац, который отличить не от чего: одна копия правится, вторая молча
    стоит рядом, и какая из них несёт тред — не доказать.
    """
    base = [_p(B), _p(A), _p(B)]        # вторая копия B стоит в стороне
    local = [_p(A), _p(B), _p(B)]
    assert engine._pair_moved_blocks([0], [(2, _p(B))], base, local) == {}


def test_pair_moved_skips_opaque_on_both_sides(engine):
    base = [{"type": "opaque", "kind": "table", "hash": "h"}]
    assert engine._pair_moved_blocks([0], [(1, _p(A))], base, [_p(A)]) == {}
    assert engine._pair_moved_blocks(
        [0], [(1, {"type": "opaque-md", "raw": "|x|"})], [_p(A)],
        [{"type": "opaque-md", "raw": "|x|"}]) == {}


def test_pair_moved_tolerates_nbsp_but_not_a_width_change(engine):
    # _norm_ws приравнивает неразрывный пробел к обычному, и ширина при этом
    # та же — переезд остаётся переездом
    base, local = [_p("Alpha beta")], [_p("Alpha\u00a0beta")]
    assert engine._pair_moved_blocks([0], [(1, local[0])], base, local) == \
        {("p", "Alpha beta"): 0}


def test_pair_moved_refuses_when_the_captured_runs_would_not_tile(engine,
                                                                 monkeypatch):
    # если нормализация когда-нибудь начнёт складывать вещи разной ширины,
    # переезд обязан перестать быть переездом, а не наложить старые runs
    # мимо границ
    monkeypatch.setattr(engine, "_norm_ws", lambda s: s.replace("  ", " "))
    base, local = [_p("Alpha  beta")], [_p("Alpha beta")]
    assert engine._pair_moved_blocks([0], [(1, local[0])], base, local) == {}


# ---------------------------------------------------------------------------
# отпечаток абзаца
# ---------------------------------------------------------------------------

def _para_el(runs):
    return make_doc_runs([runs])["body"]["content"][0]


def test_fingerprint_ignores_how_google_splits_the_runs(engine):
    """Расщепление ранов без изменения стиля — не правка коллеги.

    Docs перекраивает границы ранов сам: вставки и удаления канарейки в
    конце тела хватает, чтобы последний абзац пересобрался. Если это читать
    как чужую правку, следующий sync падает конфликтом, которого никто не
    делал, — ровно после отказа, который советует запустить снова.
    """
    one = _para_el([("Alpha beta", {})])
    two = _para_el([("Alpha ", {}), ("beta", {})])
    assert engine._para_state_fingerprint(one) == \
        engine._para_state_fingerprint(two)


def test_fingerprint_still_sees_a_real_style_change(engine):
    plain_el = _para_el([("Alpha ", {}), ("beta", {})])
    styled = _para_el([("Alpha ", {}), ("beta", RED)])
    assert engine._para_state_fingerprint(plain_el) != \
        engine._para_state_fingerprint(styled)


def test_fingerprint_sees_a_paragraph_style_change(engine):
    el = _para_el([("Alpha", {})])
    other = json.loads(json.dumps(el))
    other["paragraph"]["paragraphStyle"]["alignment"] = "CENTER"
    assert engine._para_state_fingerprint(el) != \
        engine._para_state_fingerprint(other)


def test_a_refused_sync_does_not_poison_the_next_one(engine, monkeypatch,
                                                     tmp_path, capsys):
    """Последний абзац пересобран из двух ранов в один — и правится локально.

    Это то, что оставляет за собой канарейка: расщепление ранов без единой
    правки стиля. Если читать его как правку коллеги, локальный переезд того
    же абзаца становится конфликтом, которого никто не делал, — ровно после
    отказа, который советует поправить файл и запустить снова (найдено на
    приёмке r14).
    """
    base = [A, B, C]
    local = [C, A, B]
    sidecar_doc = make_doc_runs([A, B, [("Charl", {}), ("ie", {})]])
    live = make_doc_runs([A, B, [("Charlie", {})]])
    docs = DocsStub(live, merged_doc=make_doc(local, rev="R2"))
    drive = DriveStub([], _docx_builder(docs, [(t, []) for t in base], []),
                      html=("".join(f"<p>{t}</p>" for t in local)).encode())
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, sidecar_doc, "\n\n".join(base),
                      "\n\n".join(local))
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out.get("moved") == 1, out


# ---------------------------------------------------------------------------
# арифметика запросов на перестановках
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("local", [
    [A, C, B, D],          # обмен соседей
    [B, A, C, D],          # обмен первой пары
    [A, B, D, C],          # обмен последней пары
    [D, A, B, C],          # хвост наверх
    [B, C, D, A],          # голова в хвост
    [C, D, A, B],          # два блока по два
    [D, C, B, A],          # полный разворот
])
def test_reorder_requests_produce_the_asked_order(engine, monkeypatch,
                                                  tmp_path, capsys, local):
    base = [A, B, C, D]
    docs = run_sync(engine, monkeypatch, tmp_path, base, local)
    capsys.readouterr()
    assert replay(base, text_batch(docs)) == local


def test_reorder_never_inserts_inside_a_range_it_deletes(engine, monkeypatch,
                                                         tmp_path, capsys):
    docs = run_sync(engine, monkeypatch, tmp_path, [A, B, C, D], [D, C, B, A])
    capsys.readouterr()
    reqs = text_batch(docs)
    deletes = [r["deleteContentRange"]["range"] for r in reqs
               if "deleteContentRange" in r]
    for r in reqs:
        if "insertText" not in r:
            continue
        i = r["insertText"]["location"]["index"]
        for rng in deletes:
            assert not (rng["startIndex"] < i < rng["endIndex"]), \
                f"вставка {i} внутри удаляемого {rng}"


# ---------------------------------------------------------------------------
# отчёт
# ---------------------------------------------------------------------------

def test_report_calls_a_move_a_move_once(engine, monkeypatch, tmp_path,
                                         capsys):
    run_sync(engine, monkeypatch, tmp_path, [A, B, C, D], [A, C, B, D])
    out = json.loads(capsys.readouterr().out)
    assert out["moved"] == 1
    assert out["deleted"] == 0 and out["inserted"] == 0


def test_report_still_counts_a_real_insert_and_a_real_delete(engine,
                                                             monkeypatch,
                                                             tmp_path, capsys):
    run_sync(engine, monkeypatch, tmp_path, [A, B, C], [A, C, "Echo"])
    out = json.loads(capsys.readouterr().out)
    assert out["moved"] == 0
    assert out["deleted"] == 1 and out["inserted"] == 1


def test_journal_names_what_moved(engine, monkeypatch, tmp_path, capsys):
    """Журнал восстановления должен называть переехавший блок поимённо."""
    from test_sync_anchors import http_error
    base, local = [A, B, C], [B, A, C]
    doc = make_doc(base)
    docs = DocsStub(doc, merged_doc=make_doc(local, rev="R2"))
    docs.main_error = http_error(500)
    drive = DriveStub([], _docx_builder(docs, [(t, []) for t in base], []))
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, "\n\n".join(base),
                      "\n\n".join(local))
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    journal = json.load(open(out["journal"], encoding="utf-8"))
    assert journal["plan"]["moved"] == [
        {"base_index": 1, "type": "p", "text": B}]


# ---------------------------------------------------------------------------
# оформление переезжает вместе с блоком
# ---------------------------------------------------------------------------

RED = {"foregroundColor": {"color": {"rgbColor": {"red": 0.8}}}}


def test_a_moved_block_keeps_its_own_inline_styling(engine, monkeypatch,
                                                    tmp_path, capsys):
    """Абзац с одним цветным словом переезжает и остаётся цветным.

    Ветка перезаписи здесь не годится: она сохраняет inline-оформление,
    только когда оно однородно по всему абзацу, а тут оно неоднородное —
    ровно случай замера M21.
    """
    styled = [("Bravo ", {}), ("red", RED), (" tail", {})]
    doc = make_doc_runs([A, styled, C])
    base = plain([A, styled, C])
    local = [base[1], A, C]      # difflib двигает именно оформленный абзац
    docs = run_sync(engine, monkeypatch, tmp_path, base, local, doc=doc)
    capsys.readouterr()
    colored = [r["updateTextStyle"] for r in style_batch(docs)
               if "updateTextStyle" in r
               and r["updateTextStyle"]["textStyle"].get("foregroundColor")]
    assert colored, "цвет не переехал вместе с блоком"
    span = colored[0]["range"]
    assert span["endIndex"] - span["startIndex"] == len("red")


def test_a_fresh_block_still_arrives_clean(engine, monkeypatch, tmp_path,
                                           capsys):
    """Вставка нового абзаца по-прежнему не тащит оформление соседа."""
    styled = [("Bravo ", {}), ("red", RED), (" tail", {})]
    doc = make_doc_runs([A, styled])
    base = plain([A, styled])
    docs = run_sync(engine, monkeypatch, tmp_path, base, local=base + ["Echo"],
                    doc=doc)
    capsys.readouterr()
    for r in style_batch(docs):
        ts = r.get("updateTextStyle", {}).get("textStyle", {})
        assert not ts.get("foregroundColor")


# ---------------------------------------------------------------------------
# хвост документа
# ---------------------------------------------------------------------------

def test_moving_a_block_to_the_end_leaves_no_blank_paragraph(engine,
                                                             monkeypatch,
                                                             tmp_path, capsys):
    """Документ уже кончается пустым абзацем — лишнего появиться не должно."""
    docs = run_sync(engine, monkeypatch, tmp_path, [A, B, C], [B, C, A],
                    tail_empty=True)
    capsys.readouterr()
    inserts = [r["insertText"]["text"] for r in text_batch(docs)
               if "insertText" in r]
    assert inserts and not any(t.startswith("\n") for t in inserts), \
        "ведущий перевод строки рождает пустой абзац перед вставленным блоком"


def test_appending_after_a_text_paragraph_still_starts_a_new_one(engine,
                                                                 monkeypatch,
                                                                 tmp_path,
                                                                 capsys):
    docs = run_sync(engine, monkeypatch, tmp_path, [A, B, C], [B, C, A])
    capsys.readouterr()
    inserts = [r["insertText"]["text"] for r in text_batch(docs)
               if "insertText" in r]
    assert any(t.startswith("\n") for t in inserts), \
        "без ведущего перевода строки блок приклеится к последнему абзацу"


# ---------------------------------------------------------------------------
# комментарии
# ---------------------------------------------------------------------------

def test_a_comment_beside_the_move_does_not_stop_it(engine, monkeypatch,
                                                    tmp_path, capsys):
    """Якорь на абзаце, который остаётся на месте, перестановке не мешает."""
    run_sync(engine, monkeypatch, tmp_path, [A, B, C, D], [A, C, B, D],
             comments=[api_comment("c1", "A", CREATED)], anchors={D})
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "synced" and out["moved"] == 1


def test_moving_a_commented_block_refuses_and_says_it_is_a_move(engine,
                                                                monkeypatch,
                                                                tmp_path,
                                                                capsys):
    with pytest.raises(SystemExit):
        run_sync(engine, monkeypatch, tmp_path, [A, B, C, D], [A, C, B, D],
                 comments=[api_comment("c1", "A", CREATED)], anchors={C})
    err = json.loads(capsys.readouterr().out)["error"]
    assert "переставлен" in err
    assert "`patch`" not in err, "у patch нет операции переноса"
    assert "Leave that paragraph unchanged" not in err


def test_a_remote_move_is_never_taken_for_a_local_one(engine, monkeypatch,
                                                      tmp_path, capsys):
    """Документ переставили в интерфейсе, локально не трогали — писать нечего.

    База знает порядок A B C, живой документ уже B A C. Соблазн прочитать
    это как локальный переезд и вернуть A наверх; правильный ответ — не
    делать ничего.
    """
    base, base_md = [A, B, C], "\n\n".join([A, B, C])
    live = make_doc([B, A, C])
    docs = DocsStub(live)
    drive = DriveStub([], _docx_builder(docs, [(t, []) for t in [B, A, C]], []))
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, make_doc(base), base_md, base_md)
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["noop"] is True
    assert docs.batches == []


# ---------------------------------------------------------------------------
# документ без права правки
# ---------------------------------------------------------------------------

def test_export_snapshot_survives_a_doc_without_a_revision(engine,
                                                           monkeypatch):
    """Google не отдаёт revisionId, когда правки нет — это не повод падать."""
    from test_sync_anchors import _Req
    doc = make_doc([A])
    doc.pop("revisionId")

    class Docs:
        def documents(self):
            return self

        def get(self, **kw):
            return _Req({})          # поля revisionId в ответе нет вовсе

    class Drive:
        def files(self):
            return self

        def export(self, **kw):
            return _Req(b"<p>Alpha</p>")

    monkeypatch.setattr(engine, "_safe_get_doc", lambda _d, _f: doc)
    data, got = engine._export_html_snapshot(Drive(), Docs(), "doc1")
    assert data == b"<p>Alpha</p>" and got is doc


def test_sidecar_without_a_revision_closes_sync_and_says_why(engine, tmp_path):
    doc = make_doc([A])
    doc.pop("revisionId")
    payload = engine._sidecar_payload("doc1", str(tmp_path / "d.md"), A, doc)
    assert payload["revision_id"] is None
    assert payload["sync_supported"] is False
    assert "edit" in payload["reason"]


def test_rewriting_a_commented_block_still_points_at_patch(engine, monkeypatch,
                                                           tmp_path, capsys):
    """Совет про patch верен для правки и должен остаться для неё."""
    with pytest.raises(SystemExit):
        run_sync(engine, monkeypatch, tmp_path, [A, B, C], [A, "Bravo edited",
                                                            C],
                 comments=[api_comment("c1", "A", CREATED)], anchors={B})
    err = json.loads(capsys.readouterr().out)["error"]
    assert "`patch`" in err and "переставлен" not in err


# ---------------------------------------------------------------------------
# арифметика в кодовых единицах UTF-16
# ---------------------------------------------------------------------------

def make_doc_u16(texts, rev="R0"):
    """Как make_doc, но индексы считаются в кодовых единицах UTF-16.

    make_doc берёт len() и на символах вне BMP даёт индексы, которых у Docs
    не бывает; на таком документе арифметика запросов не проверяется.
    """
    content, idx = [], 1
    for t in texts:
        width = sum(2 if ord(c) > 0xFFFF else 1 for c in t) + 1
        s, e = idx, idx + width
        content.append({"startIndex": s, "endIndex": e, "paragraph": {
            "elements": [{"startIndex": s, "endIndex": e,
                          "textRun": {"content": t + "\n", "textStyle": {}}}],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}})
        idx = e
    return {"documentId": "doc1", "revisionId": rev,
            "body": {"content": content}}


def replay_u16(texts, reqs):
    """Проиграть батч по модели, где индекс — кодовая единица UTF-16."""
    body = "".join(t + "\n" for t in texts).encode("utf-16-le")
    for r in reqs:
        if "insertText" in r:
            i = r["insertText"]["location"]["index"]
            piece = r["insertText"]["text"].encode("utf-16-le")
            assert 0 <= (i - 1) * 2 <= len(body), f"вставка вне тела: {i}"
            body = body[:(i - 1) * 2] + piece + body[(i - 1) * 2:]
        elif "deleteContentRange" in r:
            rng = r["deleteContentRange"]["range"]
            s, e = rng["startIndex"], rng["endIndex"]
            assert 0 <= (s - 1) * 2 < (e - 1) * 2 <= len(body), \
                f"удаление вне тела: {s}..{e}"
            body = body[:(s - 1) * 2] + body[(e - 1) * 2:]
        else:
            raise AssertionError(f"лишний запрос {sorted(r)}")
    out = body.decode("utf-16-le").split("\n")
    assert out[-1] == ""
    return out[:-1]


EMOJI = "Дельта 🧣 с эмодзи"


def test_reorder_counts_indices_in_utf16_units(engine, monkeypatch, tmp_path,
                                               capsys):
    """Абзац с символом вне BMP переезжает по правильным индексам."""
    base = [A, EMOJI, C]
    local = [EMOJI, A, C]
    doc = make_doc_u16(base)
    docs = DocsStub(doc, merged_doc=make_doc_u16(local, rev="R2"))
    drive = DriveStub([], _docx_builder(docs, [(t, []) for t in base], []),
                      html=("".join(f"<p>{t}</p>"
                                    for t in local)).encode())
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, "\n\n".join(base),
                      "\n\n".join(local))
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["moved"] == 1
    assert replay_u16(base, text_batch(docs)) == local


# ---------------------------------------------------------------------------
# все перестановки четырёх блоков
# ---------------------------------------------------------------------------

def _perms():
    import itertools
    base = [A, B, C, D]
    return [list(p) for p in itertools.permutations(base) if list(p) != base]


@pytest.mark.parametrize("local", _perms())
def test_every_permutation_of_four_blocks_lands_right(engine, monkeypatch,
                                                      tmp_path, capsys, local):
    base = [A, B, C, D]
    docs = run_sync(engine, monkeypatch, tmp_path, base, local)
    capsys.readouterr()
    assert replay(base, text_batch(docs)) == local


@pytest.mark.parametrize("local", [
    [B, A, D, C],                       # два независимых обмена
    [A, C, D, B],                       # средний блок через двух соседей
    [B, C, "Echo", D, A],               # переезд вместе со вставкой
    [C, A, D],                          # переезд вместе с удалением
    [A, C, "Bravo edited", D],          # переезд вместе с перезаписью
])
def test_a_move_mixed_with_other_edits_lands_right(engine, monkeypatch,
                                                   tmp_path, capsys, local):
    base = [A, B, C, D]
    docs = run_sync(engine, monkeypatch, tmp_path, base, local)
    capsys.readouterr()
    assert replay(base, text_batch(docs)) == local


# ---------------------------------------------------------------------------
# источник переезда ищется в ЖИВОМ документе, а не по индексу базы
# ---------------------------------------------------------------------------

BLUE = {"foregroundColor": {"color": {"rgbColor": {"blue": 0.9}}}}
BIG = {"fontSize": {"magnitude": 18, "unit": "PT"}}


def test_the_moved_block_is_taken_from_where_it_lies_now(engine, monkeypatch,
                                                         tmp_path, capsys):
    """Коллега добавил абзац сверху — исходник переезда сместился.

    База знает A B C D, живой документ уже X A B C D. Локально переезжает C.
    Если брать исходник по индексу базы, оформление приедет с чужого абзаца:
    в живом документе под индексом базы лежит уже сосед.
    """
    base = [A, B, C, D]
    local = [A, C, B, D]
    sidecar_doc = make_doc_runs([[(A, RED)], [(B, {})], [(C, BLUE)],
                                 [(D, {})]])
    live = make_doc_runs([[("Xray", BIG)], [(A, RED)], [(B, {})], [(C, BLUE)],
                          [(D, {})]])
    docs = DocsStub(live, merged_doc=make_doc(["Xray"] + local, rev="R2"))
    drive = DriveStub([], _docx_builder(
        docs, [(t, []) for t in ["Xray"] + base], []),
        html=("".join(f"<p>{t}</p>" for t in ["Xray"] + local)).encode())
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, sidecar_doc, "\n\n".join(base),
                      "\n\n".join(local))
    engine.sync_doc("doc1", md)
    capsys.readouterr()
    colored = [r["updateTextStyle"]["textStyle"] for r in style_batch(docs)
               if "updateTextStyle" in r
               and r["updateTextStyle"]["textStyle"].get("foregroundColor")]
    assert colored, "оформление переехавшего блока потерялось"
    assert all("blue" in ts["foregroundColor"]["color"]["rgbColor"]
               for ts in colored), \
        f"оформление приехало не с того абзаца: {colored}"


# ---------------------------------------------------------------------------
# переезд плюс локальная правка разметки
# ---------------------------------------------------------------------------

def test_a_moved_block_takes_new_marks_and_keeps_old_colour(engine,
                                                            monkeypatch,
                                                            tmp_path, capsys):
    """Блок переехал И получил жирное начертание в файле.

    Начертание — из markdown, цвет и кегль — со старого места.
    """
    styled = [("Bravo ", BIG), ("red", {**RED, **BIG}), (" tail", BIG)]
    doc = make_doc_runs([A, styled, C])
    base = plain([A, styled, C])
    local = ["**Bravo** red tail", A, C]
    docs = run_sync(engine, monkeypatch, tmp_path, base, local, doc=doc,
                    merged_texts=["Bravo red tail", A, C])
    capsys.readouterr()
    reqs = [r["updateTextStyle"] for r in style_batch(docs)
            if "updateTextStyle" in r]
    assert any(r["textStyle"].get("bold") for r in reqs), \
        "разметка из markdown не применилась"
    assert any(r["textStyle"].get("foregroundColor") for r in reqs), \
        "цвет со старого места не переехал"
    assert any(r["textStyle"].get("fontSize") for r in reqs), \
        "кегль со старого места не переехал"


def test_a_dragged_selection_over_a_move_does_not_promise_patch(engine,
                                                                monkeypatch,
                                                                tmp_path,
                                                                capsys):
    """Выделение протянуто через границу абзаца, и часть его переезжает.

    Точечная правка здесь не спасает так же, как и на целом переехавшем
    блоке, — значит и обещать `patch` нельзя.
    """
    base = ["Заголовок", "Подзаголовок", C]
    local = ["Подзаголовок", "Заголовок", C]
    doc = make_doc(base)
    docs = DocsStub(doc, merged_doc=make_doc(local, rev="R2"))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _crossing_docx(docs, base, "0", (0, 0), (1, len("Подзаголовок")),
                       [("0", "A", CREATED_SEC)]),
        html=("".join(f"<p>{t}</p>" for t in local)).encode())
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, "\n\n".join(base),
                      "\n\n".join(local))
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "несколько абзацев" in err
    assert "`patch`" not in err


def test_fingerprint_ignores_empty_runs(engine):
    """Пустой фрагмент между двумя одинаковыми — ни текста, ни вида."""
    whole = _para_el([("Alpha beta", {})])
    with_gap = _para_el([("Alpha ", {}), ("", RED), ("beta", {})])
    assert engine._para_state_fingerprint(whole) == \
        engine._para_state_fingerprint(with_gap)


def test_an_old_sidecar_is_refused_before_any_call_to_google(engine,
                                                             monkeypatch,
                                                             tmp_path, capsys):
    """Файл-база прошлой версии отвергается до похода в Google.

    Отпечатки в ней считались иначе, и если бы она дожила до сравнения,
    каждый абзац выглядел бы переписанным коллегой.
    """
    def boom():
        raise AssertionError("до Google дойти не должны были")

    monkeypatch.setattr(engine, "get_creds", boom)
    doc = make_doc([A, B])
    md = make_workdir(engine, tmp_path, doc, f"{A}\n\n{B}", f"{B}\n\n{A}")
    sidecar = md + engine.SIDECAR_SUFFIX
    payload = json.load(open(sidecar, encoding="utf-8"))
    payload["schema_version"] = 2
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    assert "schema 2 unsupported" in json.loads(capsys.readouterr().out)["error"]


def test_a_failed_style_batch_says_the_text_already_landed(engine, monkeypatch,
                                                           tmp_path, capsys):
    """Текстовый батч прошёл, стилевой упал — молчать об этом нельзя.

    Документ уже переставлен, а оформление к нему не приехало: переехавший
    блок в этот момент выглядит ровно как блок, который его молча потерял.
    """
    from test_sync_anchors import http_error
    styled = [("Bravo ", {}), ("red", RED), (" tail", {})]
    doc = make_doc_runs([A, styled, C])
    base = plain([A, styled, C])
    local = [base[1], A, C]
    docs = DocsStub(doc, merged_doc=make_doc(local, rev="R2"))
    real_batch = docs.batchUpdate

    def batch(documentId=None, body=None):
        if any("updateTextStyle" in r or "updateParagraphStyle" in r
               for r in body["requests"]):
            raise http_error(500)
        return real_batch(documentId=documentId, body=body)

    docs.batchUpdate = batch
    drive = DriveStub([], _docx_builder(docs, [(t, []) for t in base], []),
                      html=("".join(f"<p>{t}</p>" for t in local)).encode())
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, "\n\n".join(base),
                      "\n\n".join(local))
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "ТЕКСТ УЖЕ ПРИМЕНЁН" in err
    assert "переехало блоков: 1" in err


def test_utf16_arithmetic_holds_with_several_non_bmp_blocks(engine,
                                                            monkeypatch,
                                                            tmp_path, capsys):
    """Эмодзи до, внутри и после переезжающего блока, вместе с хвостом."""
    first = "🧣 Первый блок с эмодзи"
    mover = "Переезжает 🚚 сюда"
    tail = "Хвост 🏁 документа"
    base = [first, mover, C, tail]
    local = [first, C, tail, mover]        # переезд в самый конец
    doc = make_doc_u16(base)
    docs = DocsStub(doc, merged_doc=make_doc_u16(local, rev="R2"))
    drive = DriveStub([], _docx_builder(docs, [(t, []) for t in base], []),
                      html=("".join(f"<p>{t}</p>" for t in local)).encode())
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, "\n\n".join(base),
                      "\n\n".join(local))
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["moved"] == 1
    assert replay_u16(base, text_batch(docs)) == local
