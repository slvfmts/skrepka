"""Вкладки: запись обязана нести вкладку, иначе она идёт не туда.

Замерено в M19 (`internal/MEASURE-M19.md`): запрос без вкладки не нейтрален.
`insertText` и `deleteContentRange` уходят в ПЕРВУЮ вкладку — молча, если
индекс в ней законен, — а `replaceAllText` переписывает сразу ВСЕ. Поэтому
проведение вкладки проверяется здесь как поведение, а не как деталь.
"""
import json

import pytest


class _Res:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class Recorder:
    """Docs-сервис, который запоминает батчи и умеет отдать документ."""

    def __init__(self, doc=None):
        self.batches = []
        self.doc = doc

    def documents(self):
        return self

    def batchUpdate(self, documentId=None, body=None):
        self.batches.append(body)
        return _Res({"writeControl": {"requiredRevisionId": "R1"},
                     "replies": [{"replaceAllText": {
                         "occurrencesChanged": 1}}]})

    def get(self, documentId=None, fields=None, **kw):
        if fields == "revisionId":
            return _Res({"revisionId": "R1"})
        if self.doc is None:
            raise RuntimeError("чтение документа в этом тесте не ожидалось")
        return _Res(self.doc)

    @property
    def requests(self):
        return [r for b in self.batches for r in b["requests"]]


def _tab(tab_id, title, text):
    end = 1 + len(text) + 1
    return {"tabProperties": {"tabId": tab_id, "title": title},
            "documentTab": {"body": {"content": [
                {"startIndex": 1, "endIndex": end,
                 "paragraph": {"elements": [
                     {"startIndex": 1, "endIndex": end,
                      "textRun": {"content": text + "\n",
                                  "textStyle": {}}}]}}]}}}


@pytest.fixture
def two_tabs():
    """Документ из двух вкладок; целевая — ВТОРАЯ.

    Первая специально не целевая: если бы целевой была первая, снятый
    `tabId` уходил бы «в первую» = в целевую, и промах был бы невидим.
    """
    return {"revisionId": "R0",
            "tabs": [_tab("t.0", "Черновик", "первая вкладка"),
                     _tab("t.втор", "Чистовик", "вторая вкладка")]}


# --- сама ограда ---------------------------------------------------------

def test_scope_puts_the_tab_where_each_kind_carries_it(engine):
    scoped = engine._scope_requests([
        {"insertText": {"location": {"index": 5}, "text": "х"}},
        {"deleteContentRange": {"range": {"startIndex": 5, "endIndex": 7}}},
        {"updateTextStyle": {"range": {"startIndex": 5, "endIndex": 7},
                             "textStyle": {}, "fields": "bold"}},
    ], "t.втор")
    assert scoped[0]["insertText"]["location"]["tabId"] == "t.втор"
    assert scoped[1]["deleteContentRange"]["range"]["tabId"] == "t.втор"
    assert scoped[2]["updateTextStyle"]["range"]["tabId"] == "t.втор"


def test_a_request_kind_outside_the_white_list_is_refused(engine):
    """Белый список — не удобство, а ограда: запрос без известного места для
    идентификатора вкладки ушёл бы в ПЕРВУЮ вкладку молча (M19-9). После
    уборки `replaceAllText` из писателя он выпал и из списка, и попытка
    отправить его теперь останавливается, а не проходит незамеченной."""
    with pytest.raises((SystemExit, engine.PatchOpError)):
        engine._scope_requests([
            {"replaceAllText": {"containsText": {"text": "а"},
                                "replaceText": "б"}}], "t.втор")


def test_an_unknown_request_kind_is_refused_before_writing(engine):
    with pytest.raises(SystemExit):
        engine._scope_requests(
            [{"insertPageBreak": {"location": {"index": 1}}}], "t.втор")


def test_a_request_with_no_place_for_the_tab_is_refused(engine):
    with pytest.raises(SystemExit):
        engine._scope_requests([{"insertText": {"text": "х"}}], "t.втор")


def test_a_request_naming_two_kinds_is_refused(engine):
    with pytest.raises(SystemExit):
        engine._scope_requests(
            [{"insertText": {"location": {"index": 1}, "text": "х"},
              "deleteContentRange": {"range": {}}}], "t.втор")


def test_a_legacy_doc_without_tabs_is_left_alone(engine):
    req = [{"insertText": {"location": {"index": 5}, "text": "х"}}]
    assert engine._scope_requests(req, None) == req


def test_scoping_does_not_mutate_the_callers_request(engine):
    req = {"insertText": {"location": {"index": 5}, "text": "х"}}
    engine._scope_requests([req], "t.втор")
    assert "tabId" not in req["insertText"]["location"]


# --- канарейка -----------------------------------------------------------

def test_the_canary_delete_carries_the_canarys_own_tab(engine):
    req = engine._canary_delete_request(
        {"start": 10, "end": 20, "tab_id": "t.втор"})
    assert req["deleteContentRange"]["range"]["tabId"] == "t.втор"


def test_the_delete_prepended_to_the_rewrite_batch_is_scoped_too(engine):
    """Мина раунда: `extra_requests_before` шёл мимо проведения вкладки.

    Незапертое удаление сносит диапазон ПЕРВОЙ вкладки — чужой текст, — а
    канарейка остаётся на месте.
    """
    docs = Recorder()
    # ГОЛЫЙ запрос, без вкладки: проверяется договор самой функции — всё,
    # что через неё уходит, заперто, — а не то, что вызывающий уже
    # позаботился. Первая редакция теста подсовывала сюда результат
    # `_canary_delete_request`, который запирает себя сам, и мутация
    # «extra_requests_before мимо ограды» проходила незамеченной.
    naked = {"deleteContentRange": {"range": {"startIndex": 10,
                                              "endIndex": 20}}}
    engine._execute_anchor_rewrite(
        docs, "F", "t.втор",
        [{"insertText": {"location": {"index": 30}, "text": "нов"}}],
        "R1", "источник", extra_requests_before=[naked])
    sent = docs.requests
    assert sent[0]["deleteContentRange"]["range"]["tabId"] == "t.втор"
    assert sent[1]["insertText"]["location"]["tabId"] == "t.втор"


def test_cleanup_looks_for_the_canary_in_its_own_tab(engine, two_tabs):
    """Канарейка живёт во ВТОРОЙ вкладке; в первой её нет.

    Раньше уборка звала `_select_tab(tab_id=None)`, который на
    многовкладочном документе отказывает, — то есть убрать за собой
    служебную строку код не мог вовсе.
    """
    canary_text = "⚓ skrepka-canary-проба"
    doc = json.loads(json.dumps(two_tabs))
    body = doc["tabs"][1]["documentTab"]["body"]["content"]
    start = body[-1]["endIndex"]
    end = start + len(canary_text) + 1
    body.append({"startIndex": start, "endIndex": end,
                 "paragraph": {"elements": [
                     {"startIndex": start, "endIndex": end,
                      "textRun": {"content": canary_text + "\n",
                                  "textStyle": {}}}]}})
    docs = Recorder(doc)
    ok = engine._cleanup_canary(docs, "F", {
        "text": canary_text, "start": start, "end": end,
        "tab_id": "t.втор"})
    assert ok is True
    assert (docs.requests[0]["deleteContentRange"]["range"]["tabId"]
            == "t.втор")


def test_the_presence_probe_reads_the_canarys_tab(engine, two_tabs):
    canary_text = "⚓ skrepka-canary-проба"
    doc = json.loads(json.dumps(two_tabs))
    body = doc["tabs"][1]["documentTab"]["body"]["content"]
    start = body[-1]["endIndex"]
    end = start + len(canary_text) + 1
    body.append({"startIndex": start, "endIndex": end,
                 "paragraph": {"elements": [
                     {"startIndex": start, "endIndex": end,
                      "textRun": {"content": canary_text + "\n",
                                  "textStyle": {}}}]}})
    assert engine._canary_present(Recorder(doc), "F", {
        "text": canary_text, "start": start, "end": end,
        "tab_id": "t.втор"}) is True


def test_the_canary_insert_names_the_target_tab(engine, two_tabs):
    """Индекс канарейки считан по целевой вкладке — запрос обязан её нести.

    Выгрузка в этом тесте падает: важен только первый ушедший батч.
    """
    class Drive:
        def files(self):
            return self

        def export(self, **kw):
            raise RuntimeError("выгрузка недоступна")

    docs = Recorder(two_tabs)
    with pytest.raises(SystemExit):
        engine._fresh_anchor_snapshot(
            docs, Drive(), "F", two_tabs,
            two_tabs["tabs"][1]["documentTab"], [], [], 30,
            fp1="fp", universe={}, tid="t.втор")
    insert = docs.requests[0]["insertText"]
    assert insert["location"]["tabId"] == "t.втор"
    assert "skrepka-canary" in insert["text"]


# --- доказательство отрезка вкладки ------------------------------------

def _tab_body(tab_id, title, texts):
    """documentTab с перечисленными абзацами (последний обычно пустой)."""
    content, idx = [], 1
    for t in texts:
        end = idx + len(t) + 1
        content.append({"startIndex": idx, "endIndex": end,
                        "paragraph": {"elements": [
                            {"startIndex": idx, "endIndex": end,
                             "textRun": {"content": t + "\n",
                                         "textStyle": {}}}]}})
        idx = end
    return {"tabProperties": {"tabId": tab_id, "title": title},
            "documentTab": {"body": {"content": content}}}


def _head(title):
    return (f'<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr>'
            f'<w:r><w:t>{title}</w:t></w:r></w:p>')


def _para(text, comment=None):
    if comment is None:
        return f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'
    return (f'<w:p><w:commentRangeStart w:id="{comment}"/>'
            f'<w:r><w:t>{text}</w:t></w:r>'
            f'<w:commentRangeEnd w:id="{comment}"/></w:p>')


@pytest.fixture
def twin_doc(engine, make_docx):
    """Две вкладки с ДОСЛОВНО одинаковым абзацем, комментарий в каждой.

    Ровно замеренный случай M19-18: без доказательства отрезка мэппер
    кладёт оба якоря в одну вкладку.
    """
    tabs = [("t.0", "Черновик"), ("t.втор", "Чистовик")]
    api = [_tab_body(tid, title, ["ПОВТОР", ""]) for tid, title in tabs]
    body = (_head("Черновик") + _para("ПОВТОР", comment=0) + "<w:p/>"
            + _head("Чистовик") + _para("ПОВТОР", comment=1) + "<w:p/>")
    blob = make_docx(body)
    spans, problems, census = engine._parse_docx_anchor_spans(blob)
    assert not problems and len(spans) == 2
    return engine._collect_tabs({"tabs": api}), spans, census


def test_the_segment_of_the_second_tab_is_proven(engine, twin_doc):
    tabs, _spans, census = twin_doc
    first, last, why = engine._prove_target_segment(
        census["outline"], tabs, "t.втор")
    assert why is None
    assert (first, last) == (3, 5)


def test_a_foreign_anchor_no_longer_lands_in_the_target_tab(engine, twin_doc):
    """Регрессия на M19-18 — замерено на живом документе.

    Оба абзаца дословно одинаковы, поэтому старый мэппер размещал ОБА
    якоря в целевой вкладке: чужой тред получал ограду вокруг чужого
    текста, а настоящий оставался открытым.
    """
    tabs, spans, census = twin_doc
    target = [t for t in tabs if t[0] == "t.втор"][0]
    naive, _pr, _amb = engine._map_anchors_to_doc(target[2], spans)
    assert sorted(a[3] for a in naive) == ["0", "1"], "замер: было именно так"

    first, last, why = engine._prove_target_segment(
        census["outline"], tabs, "t.втор")
    assert why is None
    mine, foreign, why2 = engine._confine_spans_to_segment(spans, first, last)
    assert why2 is None
    assert [s["docx_id"] for s in mine] == ["1"]
    assert [s["docx_id"] for s in foreign] == ["0"]
    placed, _pr, _amb = engine._map_anchors_to_doc(target[2], mine)
    assert [a[3] for a in placed] == ["1"]


def test_a_span_astride_the_boundary_is_refused_not_guessed(engine):
    spans = [{"docx_id": "7", "top": 2, "end_top": 9}]
    mine, foreign, why = engine._confine_spans_to_segment(spans, 3, 5)
    assert mine is None and foreign is None
    assert "через границу вкладки" in why


def test_an_unattached_span_is_refused(engine):
    spans = [{"docx_id": "7", "top": None, "end_top": None}]
    _m, _f, why = engine._confine_spans_to_segment(spans, 0, 2)
    assert why and "не привязан" in why


def test_a_tab_without_a_title_cannot_be_proven(engine, twin_doc):
    tabs, _spans, census = twin_doc
    tabs = [(t, "" if t == "t.втор" else title, dt) for t, title, dt in tabs]
    _f, _l, why = engine._prove_target_segment(census["outline"], tabs,
                                               "t.втор")
    assert why and "нет названия" in why


def test_a_title_that_also_stands_as_text_elsewhere_is_no_trap(engine,
                                                              make_docx):
    """Название вкладки может дословно совпадать с абзацем другой (M19-22).

    Требовать единственного вхождения заголовка нельзя: документ, на
    котором сходился прототип замера, движок бы отказал. Поэтому каждый
    кандидат проходится целиком, и годным должен оказаться ровно один.
    """
    api = [_tab_body("t.0", "Черновик", ["Чистовик", ""]),
           _tab_body("t.втор", "Чистовик", ["текст", ""])]
    body = (_head("Черновик") + _para("Чистовик") + "<w:p/>"
            + _head("Чистовик") + _para("текст") + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    first, last, why = engine._prove_target_segment(census["outline"], tabs,
                                                    "t.втор")
    assert why is None
    assert (first, last) == (3, 5)


def test_a_tab_whose_title_is_nowhere_in_the_export(engine, make_docx):
    api = [_tab_body("t.0", "Черновик", ["раз", ""]),
           _tab_body("t.втор", "Чистовик", ["два", ""])]
    body = _head("Черновик") + _para("раз") + "<w:p/>"
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    _f, _l, why = engine._prove_target_segment(census["outline"], tabs,
                                               "t.втор")
    assert why and "нет абзаца с названием вкладки" in why


def test_a_paragraph_that_differs_breaks_the_proof(engine, make_docx):
    api = [_tab_body("t.0", "Черновик", ["раз", ""]),
           _tab_body("t.втор", "Чистовик", ["два", ""])]
    body = (_head("Черновик") + _para("раз") + "<w:p/>"
            + _head("Чистовик") + _para("ТРИ") + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    _f, _l, why = engine._prove_target_segment(census["outline"], tabs,
                                               "t.втор")
    assert why and "текст расходится" in why


def test_a_soft_break_does_not_cost_the_tab_its_identity(engine, make_docx):
    """`w:br` в выгрузке против `\\v` в API (#27) — известная пара, не догадка."""
    api = [_tab_body("t.0", "Черновик", ["раз", ""]),
           _tab_body("t.втор", "Чистовик", ["две\vстроки", ""])]
    body = (_head("Черновик") + _para("раз") + "<w:p/>"
            + _head("Чистовик")
            + '<w:p><w:r><w:t>две</w:t><w:br/><w:t>строки</w:t></w:r></w:p>'
            + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    first, last, why = engine._prove_target_segment(census["outline"], tabs,
                                                   "t.втор")
    assert why is None and (first, last) == (3, 5)


def test_the_canary_is_the_only_extra_element_allowed(engine, make_docx):
    api = [_tab_body("t.0", "Черновик", ["раз", ""]),
           _tab_body("t.втор", "Чистовик", ["два", ""])]
    canary = "⚓ skrepka-canary-abc"
    body = (_head("Черновик") + _para("раз") + "<w:p/>"
            + _head("Чистовик") + _para("два") + "<w:p/>" + _para(canary))
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    first, last, why = engine._prove_target_segment(
        census["outline"], tabs, "t.втор", canary_text=canary)
    assert why is None and last == 6

    # чужой лишний абзац на месте канарейки — не проходит
    _f, _l, why2 = engine._prove_target_segment(
        census["outline"], tabs, "t.втор", canary_text="⚓ другая строка")
    assert why2 and "канарейка не нашлась" in why2

    # и без канарейки этот же документ не сходится: остался лишний элемент
    _f2, _l2, why3 = engine._prove_target_segment(census["outline"], tabs,
                                                 "t.втор")
    assert why3 and "лишних элементов" in why3


def _tab_with_table(tab_id, title, cell_text):
    """Вкладка: таблица 1×1 с текстом и пустой абзац следом."""
    cell_end = 4 + len(cell_text) + 1
    return {"tabProperties": {"tabId": tab_id, "title": title},
            "documentTab": {"body": {"content": [
                {"startIndex": 1, "endIndex": 20, "table": {
                    "tableRows": [{"startIndex": 2, "endIndex": 19,
                                   "tableCells": [
                                       {"startIndex": 3, "endIndex": 18,
                                        "content": [
                                            {"startIndex": 4,
                                             "endIndex": cell_end,
                                             "paragraph": {"elements": [
                                                 {"startIndex": 4,
                                                  "endIndex": cell_end,
                                                  "textRun": {
                                                      "content":
                                                          cell_text + "\n"}}]}}
                                        ]}]}]}},
                {"startIndex": 20, "endIndex": 21,
                 "paragraph": {"elements": [
                     {"startIndex": 20, "endIndex": 21,
                      "textRun": {"content": "\n"}}]}}]}}}


def test_a_cell_anchor_in_the_second_tab_is_placed(engine, make_docx):
    """Решётка считается ВНУТРИ отрезка — иначе замеренное M19-19.

    Выгрузка видит две таблицы (по одной в каждой вкладке), API целевой
    вкладки — одну. Без перенумерации `_resolve_cell` отказывает по
    расхождению, которого на самом деле нет.
    """
    api = [_tab_with_table("t.0", "Черновик", "Ячейка А"),
           _tab_with_table("t.втор", "Чистовик", "До якорь")]
    tbl_a = ('<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Ячейка А</w:t></w:r>'
             '</w:p></w:tc></w:tr></w:tbl>')
    tbl_b = ('<w:tbl><w:tr><w:tc><w:p><w:r><w:t>До </w:t></w:r>'
             '<w:commentRangeStart w:id="3"/><w:r><w:t>якорь</w:t></w:r>'
             '<w:commentRangeEnd w:id="3"/></w:p></w:tc></w:tr></w:tbl>')
    body = (_head("Черновик") + tbl_a + "<w:p/>"
            + _head("Чистовик") + tbl_b + "<w:p/>")
    spans, problems, census = engine._parse_docx_anchor_spans(make_docx(body))
    assert problems == []
    tabs = engine._collect_tabs({"tabs": api})
    first, last, why = engine._prove_target_segment(census["outline"], tabs,
                                                    "t.втор")
    assert why is None
    mine, _foreign, why2 = engine._confine_spans_to_segment(spans, first, last)
    assert why2 is None and len(mine) == 1
    local, why3 = engine._localize_spans(mine, census["outline"], first, last,
                                        census["docx_lattice"])
    assert why3 is None
    sp = local[0]
    assert sp["path"] == ((0, 0, 0),), "таблица перенумерована в свой отрезок"
    target = [t for t in tabs if t[0] == "t.втор"][0]
    ranges, mproblems, ambiguous = engine._map_anchors_to_doc(target[2], [sp])
    assert mproblems == [] and ambiguous == []
    assert ranges == [(7, 12, "якорь", "3")]


# --- связка целиком: снимок, канарейка, отрезок -------------------------

class _Drive:
    def __init__(self, comments, docx):
        self._comments, self._docx = comments, docx

    def comments(self):
        return self

    def list(self, **kw):
        return _Res({"comments": json.loads(json.dumps(self._comments))})

    def files(self):
        return self

    def export(self, fileId=None, mimeType=None):
        return _Res(self._docx)


class _Docs(Recorder):
    def get(self, documentId=None, fields=None, **kw):
        if fields == "revisionId":
            return _Res({"revisionId": "R1"})
        return _Res(self.doc)


def test_the_snapshot_keeps_only_the_target_tabs_anchors(engine, monkeypatch):
    """Связка целиком на многовкладочном документе.

    Тот же дословный двойник в двух вкладках, по комментарию в каждой.
    Снимок обязан отдать якорь ТОЛЬКО целевой вкладки: чужой доказанно
    недостижим для запертой записи, а размещать его в целевой — это
    замеренная ошибка M19-18.
    """
    from test_sync_anchors import make_docx_full

    canary_seen = {}

    def build_docx():
        paras = [("Черновик", []), ("ПОВТОР", [("0", 0, 6)]), ("", []),
                 ("Чистовик", []), ("ПОВТОР", [("1", 0, 6)]), ("", [])]
        if canary_seen.get("text"):
            paras.append((canary_seen["text"], []))
        return make_docx_full(paras, [("0", "Аня", "2026-08-18T10:00:00Z"),
                                      ("1", "Боря", "2026-08-18T11:00:00Z")])

    doc = {"revisionId": "R0",
           "tabs": [_tab_body("t.0", "Черновик", ["ПОВТОР", ""]),
                    _tab_body("t.втор", "Чистовик", ["ПОВТОР", ""])]}
    comments = [
        {"id": "c0", "content": "раз", "author": {"displayName": "Аня"},
         "createdTime": "2026-08-18T10:00:00Z", "replies": [],
         "quotedFileContent": {"value": "ПОВТОР"}},
        {"id": "c1", "content": "два", "author": {"displayName": "Боря"},
         "createdTime": "2026-08-18T11:00:00Z", "replies": [],
         "quotedFileContent": {"value": "ПОВТОР"}},
    ]

    class Docs(_Docs):
        def batchUpdate(self, documentId=None, body=None):
            self.batches.append(body)
            req = body["requests"][0]
            if "insertText" in req:
                canary_seen["text"] = req["insertText"]["text"].lstrip("\n")
            return _Res({"writeControl": {"requiredRevisionId": "R1"}})

    docs = Docs(doc)
    drive = _Drive(comments, None)
    drive.export = lambda fileId=None, mimeType=None: _Res(build_docx())
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)

    _all, anchored, fp1, universe = engine._census_comments(drive, "F")
    target = doc["tabs"][1]["documentTab"]
    body_end = target["body"]["content"][-1]["endIndex"]
    snap, why = engine._fresh_anchor_snapshot(
        docs, drive, "F", doc, target, anchored, [], body_end,
        fp1=fp1, universe=universe, tid="t.втор")
    assert why is None, why
    assert [a[3] for a in snap["anchors"]] == ["1"]
    assert snap["canary"]["tab_id"] == "t.втор"
    insert = docs.batches[0]["requests"][0]["insertText"]
    assert insert["location"]["tabId"] == "t.втор"


# --- узкие места: призрак и метки ---------------------------------------

def test_a_thread_alive_in_another_tab_is_not_called_a_ghost(engine):
    """Знак «текста больше нет» — про ДОКУМЕНТ, а не про целевую вкладку.

    Читая только целевую вкладку, вердикт объявляет мёртвым любой тред
    соседней: цитаты здесь нет, ограда выходит пустой, и человеку пишут,
    что его комментарий пропал, снимая с него защиту.
    """
    thread = {"id": "c9", "createdTime": "2026-08-18T10:00:00Z",
              "quotedFileContent": {"value": "ПОВТОР"}}
    records = [{"author": "Аня", "date_sec": "2026-08-18T12:00:00Z"}]
    target = _tab_body("t.втор", "Чистовик", ["другое", ""])["documentTab"]
    neighbour = _tab_body("t.0", "Черновик", ["ПОВТОР", ""])["documentTab"]

    alone = engine._ghost_verdict(thread, records, target, "F")
    assert alone is not None, "без соседей это ровно призрак"

    with_neighbour = engine._ghost_verdict(thread, records, target, "F",
                                           other_tabs=[neighbour])
    assert with_neighbour is None


def test_mark_sends_its_request_through_the_write_gate(engine, monkeypatch):
    doc = {"revisionId": "R0",
           "tabs": [_tab_body("t.0", "Черновик", ["раз", ""]),
                    _tab_body("t.втор", "Чистовик", ["цель", ""])]}
    docs = _Docs(doc)

    class Drive:
        def comments(self):
            return self

        def list(self, **kw):
            return _Res({"comments": []})

    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda c: docs)
    monkeypatch.setattr(engine, "get_drive_service", lambda c: Drive())
    monkeypatch.setattr(engine, "_print_json", lambda payload: None,
                        raising=False)
    try:
        engine.mark_range("F", "метка", "цель", tab_id="t.втор")
    except SystemExit:
        pass
    named = [r for b in docs.batches for r in b["requests"]
             if "createNamedRange" in r]
    assert named, "запрос createNamedRange не ушёл"
    assert named[0]["createNamedRange"]["range"]["tabId"] == "t.втор"


def test_sync_still_refuses_a_multi_tab_document(engine, monkeypatch,
                                                tmp_path, capsys):
    """Гейт `sync` снимать нельзя: его запросы вкладку не несут.

    Документ на момент выгрузки был одновкладочным — сайдкар честный, — а
    к моменту синхронизации в нём появилась вторая вкладка. Это и есть
    единственный путь, на котором работает именно ЭТОТ гейт: при
    изначально многовкладочном документе раньше отказывает сайдкар.

    Две предыдущие редакции теста ловили чужой отказ (нет сайдкара) и
    проходили даже со снятым гейтом. Второй раз это заметил не стенд
    мутаций, а обычный прогон: у стенда была красная база, а я обрезал
    вывод и её не увидел.
    """
    from test_sync_anchors import make_doc, make_workdir

    single = make_doc(["раз", "два"])
    md = make_workdir(engine, tmp_path, single, "раз\n\nдва\n",
                      "раз\n\nтри\n")
    multi = {"documentId": "doc1", "revisionId": "R0",
             "tabs": [_tab_body("t.0", "Черновик", ["раз", "два", ""]),
                      _tab_body("t.втор", "Чистовик", ["другое", ""])]}
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda c: _Docs(multi))
    monkeypatch.setattr(engine, "get_drive_service", lambda c: object())
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    said = capsys.readouterr().out
    assert "multi-tab" in said and "single-tab only" in said


# --- находки финального круга -------------------------------------------

def test_cleanup_refuses_when_the_canary_is_not_alone_in_its_line(engine):
    """Уборка удаляла [s-1, e), считая, что перед строкой её перевод.

    Между вставкой и уборкой человек может слить строки или напечатать
    перед служебной строкой — тогда прежний код удалял его символ вместе
    со своим. Теперь не удаляет ничего и честно возвращается ни с чем.
    """
    canary_text = "⚓ skrepka-canary-проба"
    doc = {"revisionId": "R0",
           "tabs": [_tab_body("t.0", "Черновик", ["раз", ""]),
                    _tab_body("t.втор", "Чистовик",
                              ["Хвост" + canary_text, ""])]}
    docs = _Docs(doc)
    target = doc["tabs"][1]["documentTab"]
    start, end = engine._find_quote_in_doctab(target, canary_text)
    ok = engine._cleanup_canary(docs, "F", {
        "text": canary_text, "start": start, "end": end, "tab_id": "t.втор"})
    assert ok is False
    assert docs.batches == [], "ничего не должно быть отправлено"


def test_a_row_of_a_different_width_breaks_the_proof(engine, make_docx):
    """Сверять только названные ячейки мало: решётка должна сойтись вся."""
    api = [_tab_body("t.0", "Черновик", ["раз", ""]),
           {"tabProperties": {"tabId": "t.втор", "title": "Чистовик"},
            "documentTab": {"body": {"content": [
                {"startIndex": 1, "endIndex": 30, "table": {"tableRows": [
                    {"startIndex": 2, "endIndex": 29, "tableCells": [
                        {"startIndex": 3, "endIndex": 12, "content": [
                            {"startIndex": 4, "endIndex": 9, "paragraph": {
                                "elements": [{"startIndex": 4, "endIndex": 9,
                                              "textRun": {
                                                  "content": "ячейка\n"}}]}}]},
                        {"startIndex": 13, "endIndex": 20, "content": [
                            {"startIndex": 14, "endIndex": 19, "paragraph": {
                                "elements": [{"startIndex": 14,
                                              "endIndex": 19,
                                              "textRun": {
                                                  "content": "вторая\n"}}]}}]},
                    ]}]}},
                {"startIndex": 30, "endIndex": 31, "paragraph": {"elements": [
                    {"startIndex": 30, "endIndex": 31,
                     "textRun": {"content": "\n"}}]}}]}}}]
    # в выгрузке строка ОДНА ячейка шириной 1, а по API их две
    tbl = ('<w:tbl><w:tr><w:tc><w:p><w:r><w:t>ячейка</w:t></w:r>'
           '</w:p></w:tc></w:tr></w:tbl>')
    body = (_head("Черновик") + _para("раз") + "<w:p/>"
            + _head("Чистовик") + tbl + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    _f, _l, why = engine._prove_target_segment(census["outline"], tabs,
                                               "t.втор")
    assert why and "клеток сетки" in why


def test_a_thread_living_only_in_another_tab_does_not_block(engine):
    """Тред, пропавший из выгрузки, но чей текст жив в СОСЕДНЕЙ вкладке.

    Правка, запертая в целевой вкладке, до него не дотянется, поэтому он
    не призрак (человеку не говорят, что комментарий исчез) и не повод
    отказывать.
    """
    thread = {"id": "c9", "createdTime": "2026-08-18T10:00:00Z",
              "author": {"displayName": "Аня"}, "replies": [],
              "quotedFileContent": {"value": "ПОВТОР"}}
    target = _tab_body("t.втор", "Чистовик", ["другое", ""])["documentTab"]
    neighbour = _tab_body("t.0", "Черновик", ["ПОВТОР", ""])["documentTab"]
    key = ("Аня", engine._trunc_seconds("2026-08-18T10:00:00Z"))
    universe = {key: {"c9"}}

    problems, metrics = engine._account_anchored_comments(
        [thread], [], [], universe=universe, file_id="F", doc_tab=target,
        other_tabs=[neighbour])
    assert problems == []
    assert metrics["threads_in_other_tabs"] == 1

    # тот же тред, но его текст стоит и в целевой вкладке — отказ остаётся
    target2 = _tab_body("t.втор", "Чистовик", ["ПОВТОР", ""])["documentTab"]
    problems2, _m = engine._account_anchored_comments(
        [thread], [], [], universe=universe, file_id="F", doc_tab=target2,
        other_tabs=[neighbour])
    assert any("missing from the export" in str(p) for p in problems2)


def test_a_hidden_api_cell_with_text_breaks_the_proof(engine, make_docx):
    """Ширины сошлись, а под объединением прячется текст.

    Выгрузка объединённую клетку опускает и ставит `gridSpan`, API держит
    её пустой заглушкой (M16). Заглушка с ТЕКСТОМ значит, что две стороны
    описывают разные таблицы, и границу вкладки доказывать нечем.
    """
    def _cell(start, text):
        end = start + len(text) + 1
        return {"startIndex": start - 1, "endIndex": end + 1, "content": [
            {"startIndex": start, "endIndex": end, "paragraph": {"elements": [
                {"startIndex": start, "endIndex": end,
                 "textRun": {"content": text + "\n"}}]}}]}

    api = [_tab_body("t.0", "Черновик", ["раз", ""]),
           {"tabProperties": {"tabId": "t.втор", "title": "Чистовик"},
            "documentTab": {"body": {"content": [
                {"startIndex": 1, "endIndex": 60, "table": {"tableRows": [
                    {"startIndex": 2, "endIndex": 59, "tableCells": [
                        _cell(4, "объединённая"),
                        _cell(30, "неожиданный текст")]}]}},
                {"startIndex": 60, "endIndex": 61, "paragraph": {"elements": [
                    {"startIndex": 60, "endIndex": 61,
                     "textRun": {"content": "\n"}}]}}]}}}]
    tbl = ('<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
           '<w:p><w:r><w:t>объединённая</w:t></w:r></w:p></w:tc></w:tr>'
           '</w:tbl>')
    body = (_head("Черновик") + _para("раз") + "<w:p/>"
            + _head("Чистовик") + tbl + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    _f, _l, why = engine._prove_target_segment(census["outline"], tabs,
                                               "t.втор")
    assert why and "выгрузка её не" in why


def test_a_picture_in_the_target_tab_does_not_break_the_proof(engine,
                                                              make_docx):
    """Картинка в абзаце — норма редакторского документа.

    Ни выгрузка, ни документ не кладут в такой абзац текста, поэтому по
    тексту он сходится. Якорь внутри него отклоняется своим слоем ниже —
    но вкладка целиком из-за картинки падать не должна.
    """
    api = [_tab_body("t.0", "Черновик", ["раз", ""]),
           {"tabProperties": {"tabId": "t.втор", "title": "Чистовик"},
            "documentTab": {"body": {"content": [
                {"startIndex": 1, "endIndex": 3, "paragraph": {"elements": [
                    {"startIndex": 1, "endIndex": 2,
                     "inlineObjectElement": {"inlineObjectId": "kix.1"}},
                    {"startIndex": 2, "endIndex": 3,
                     "textRun": {"content": "\n"}}]}},
                {"startIndex": 3, "endIndex": 4, "paragraph": {"elements": [
                    {"startIndex": 3, "endIndex": 4,
                     "textRun": {"content": "\n"}}]}}]}}}]
    body = (_head("Черновик") + _para("раз") + "<w:p/>"
            + _head("Чистовик")
            + '<w:p><w:r><w:drawing/></w:r></w:p>' + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    first, last, why = engine._prove_target_segment(census["outline"], tabs,
                                                    "t.втор")
    assert why is None and (first, last) == (3, 5)


def test_the_middle_tab_is_bounded_by_the_next_title(engine, make_docx):
    """Целевая вкладка не последняя: хвост отрезка пинится следующим заголовком."""
    api = [_tab_body("t.0", "Первая", ["раз", ""]),
           _tab_body("t.средн", "Средняя", ["два", ""]),
           _tab_body("t.трет", "Третья", ["три", ""])]
    body = (_head("Первая") + _para("раз") + "<w:p/>"
            + _head("Средняя") + _para("два") + "<w:p/>"
            + _head("Третья") + _para("три") + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    first, last, why = engine._prove_target_segment(census["outline"], tabs,
                                                    "t.средн")
    assert why is None and (first, last) == (3, 5)

    # заголовок следующей вкладки на месте не тот — граница не доказана
    body2 = (_head("Первая") + _para("раз") + "<w:p/>"
             + _head("Средняя") + _para("два") + "<w:p/>"
             + _para("Не заголовок") + _para("три") + "<w:p/>")
    _sp2, _pr2, census2 = engine._parse_docx_anchor_spans(make_docx(body2))
    _f, _l, why2 = engine._prove_target_segment(census2["outline"], tabs,
                                                "t.средн")
    assert why2 and "ожидался заголовок следующей вкладки" in why2


def test_cells_are_compared_by_text_not_only_by_shape(engine, make_docx):
    """Урок r11: совпадение формы не доказывает, что таблица та же."""
    api = [_tab_body("t.0", "Черновик", ["раз", ""]),
           _tab_with_table("t.втор", "Чистовик", "по документу")]
    tbl = ('<w:tbl><w:tr><w:tc><w:p><w:r><w:t>по выгрузке</w:t></w:r>'
           '</w:p></w:tc></w:tr></w:tbl>')
    body = (_head("Черновик") + _para("раз") + "<w:p/>"
            + _head("Чистовик") + tbl + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    _f, _l, why = engine._prove_target_segment(census["outline"], tabs,
                                               "t.втор")
    assert why and "расходится" in why


def test_a_foreign_tabs_broken_anchor_does_not_block_the_target(engine,
                                                               monkeypatch):
    """Комментарий соседней вкладки на абзаце с картинкой.

    Такой якорь неразмещаем, и раньше он отказывал ВСЕМУ документу —
    конфайн спасал только размещение, а проблема разбора шла мимо него.
    Правка в целевой вкладке до чужого якоря дотянуться не может.
    """
    canary_seen = {}

    def build_docx():
        import io
        import zipfile
        WORDML = ("http://schemas.openxmlformats.org/wordprocessingml/"
                  "2006/main")
        paras = [
            _head("Черновик"),
            ('<w:p><w:commentRangeStart w:id="0"/><w:r><w:drawing/></w:r>'
             '<w:r><w:t>с картинкой</w:t></w:r>'
             '<w:commentRangeEnd w:id="0"/></w:p>'),
            "<w:p/>",
            _head("Чистовик"),
            _para("цель", comment=1),
            "<w:p/>",
        ]
        if canary_seen.get("text"):
            paras.append(_para(canary_seen["text"]))
        document = (f'<?xml version="1.0"?><w:document xmlns:w="{WORDML}">'
                    f'<w:body>{"".join(paras)}</w:body></w:document>')
        comments = (
            f'<?xml version="1.0"?><w:comments xmlns:w="{WORDML}">'
            f'<w:comment w:id="0" w:author="Аня" '
            f'w:date="2026-08-18T10:00:00Z"><w:p><w:r><w:t>c</w:t></w:r>'
            f'</w:p></w:comment>'
            f'<w:comment w:id="1" w:author="Боря" '
            f'w:date="2026-08-18T11:00:00Z"><w:p><w:r><w:t>c</w:t></w:r>'
            f'</w:p></w:comment></w:comments>')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", document)
            z.writestr("word/comments.xml", comments)
        return buf.getvalue()

    picture_tab = {"tabProperties": {"tabId": "t.0", "title": "Черновик"},
                   "documentTab": {"body": {"content": [
                       {"startIndex": 1, "endIndex": 14,
                        "paragraph": {"elements": [
                            {"startIndex": 1, "endIndex": 2,
                             "inlineObjectElement": {"inlineObjectId": "o"}},
                            {"startIndex": 2, "endIndex": 14,
                             "textRun": {"content": "с картинкой\n"}}]}},
                       {"startIndex": 14, "endIndex": 15,
                        "paragraph": {"elements": [
                            {"startIndex": 14, "endIndex": 15,
                             "textRun": {"content": "\n"}}]}}]}}}
    doc = {"revisionId": "R0",
           "tabs": [picture_tab, _tab_body("t.втор", "Чистовик",
                                           ["цель", ""])]}
    comments = [
        {"id": "c0", "content": "раз", "author": {"displayName": "Аня"},
         "createdTime": "2026-08-18T10:00:00Z", "replies": [],
         "quotedFileContent": {"value": "с картинкой"}},
        {"id": "c1", "content": "два", "author": {"displayName": "Боря"},
         "createdTime": "2026-08-18T11:00:00Z", "replies": [],
         "quotedFileContent": {"value": "цель"}},
    ]

    class Docs(_Docs):
        def batchUpdate(self, documentId=None, body=None):
            self.batches.append(body)
            req = body["requests"][0]
            if "insertText" in req:
                canary_seen["text"] = req["insertText"]["text"].lstrip("\n")
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
    assert [a[3] for a in snap["anchors"]] == ["1"]


def test_a_table_with_a_different_number_of_rows_breaks_the_proof(engine,
                                                                  make_docx):
    api = [_tab_body("t.0", "Черновик", ["раз", ""]),
           _tab_with_table("t.втор", "Чистовик", "ячейка")]
    # в выгрузке таблица ДВУХ строк, по документу — одной
    tbl = ('<w:tbl><w:tr><w:tc><w:p><w:r><w:t>ячейка</w:t></w:r></w:p>'
           '</w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>лишняя</w:t></w:r>'
           '</w:p></w:tc></w:tr></w:tbl>')
    body = (_head("Черновик") + _para("раз") + "<w:p/>"
            + _head("Чистовик") + tbl + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    _f, _l, why = engine._prove_target_segment(census["outline"], tabs,
                                               "t.втор")
    assert why and "строк в таблице" in why


def test_a_paragraph_where_a_table_is_expected_breaks_the_proof(engine,
                                                                make_docx):
    api = [_tab_body("t.0", "Черновик", ["раз", ""]),
           _tab_with_table("t.втор", "Чистовик", "ячейка")]
    body = (_head("Черновик") + _para("раз") + "<w:p/>"
            + _head("Чистовик") + _para("ячейка") + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    _f, _l, why = engine._prove_target_segment(census["outline"], tabs,
                                               "t.втор")
    assert why and "по документу это таблица" in why
