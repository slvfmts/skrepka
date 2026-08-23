"""r13 — мягкий перенос как полноправный символ (#27, #40).

Замер, на котором стоят эти тесты, — `internal/MEASURE-M20.md`. Главный его
факт: выгрузка пишет мягкий перенос и разрыв страницы РАЗНЫМИ атрибутами
одного тега, а простая нормализация `\\v` -> `\\n` даёт ложное совпадение на
двойниках (M20-4). Поэтому обе стороны говорят одним алфавитом.
"""


def _u16(text):
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def _tab(paragraphs):
    """documentTab из списка абзацев; str — прогон текста, dict — элемент."""
    content, at = [], 1
    for parts in paragraphs:
        elements, start = [], at
        for part in list(parts) + ["\n"]:
            if isinstance(part, str):
                width = _u16(part)
                elements.append({"startIndex": at, "endIndex": at + width,
                                 "textRun": {"content": part}})
            else:
                width = part.pop("_width", 1)
                elements.append(dict({"startIndex": at, "endIndex": at + width},
                                     **part))
            at += width
        content.append({"startIndex": start, "endIndex": at,
                        "paragraph": {"elements": elements}})
    return {"body": {"content": content}}


def _anchored(text, cid="0"):
    return (f'<w:p><w:commentRangeStart w:id="{cid}"/>'
            f'<w:r><w:t>{text}</w:t></w:r>'
            f'<w:commentRangeEnd w:id="{cid}"/></w:p>')


def _break_para(head, tail, kind=None, cid=None):
    """Абзац head<br/>tail, при cid — целиком под якорем."""
    attr = "" if kind is None else f' w:type="{kind}"'
    inner = (f'<w:r><w:t>{head}</w:t></w:r><w:r><w:br{attr}/></w:r>'
             f'<w:r><w:t>{tail}</w:t></w:r>')
    if cid is None:
        return f'<w:p>{inner}</w:p>'
    return (f'<w:p><w:commentRangeStart w:id="{cid}"/>{inner}'
            f'<w:commentRangeEnd w:id="{cid}"/></w:p>')


# ---------------------------------------------------------------------------
# сторона выгрузки: вид разрыва больше не теряется
# ---------------------------------------------------------------------------

def test_a_soft_break_is_read_the_way_the_api_spells_it(engine, make_docx):
    """`w:br` без атрибута и `w:br type="textWrapping"` — один и тот же мягкий
    перенос, и обе формы читаются как `\\v`, которым его отдаёт API."""
    for kind in (None, "textWrapping"):
        spans, problems, _c = engine._parse_docx_anchor_spans(
            make_docx(_break_para("первая", "вторая", kind=kind, cid="0")))
        assert problems == []
        assert spans[0]["para_text"] == "первая\vвторая"


def test_a_page_break_is_not_confused_with_a_soft_one(engine, make_docx):
    """Ровно то, что делает нормализацию небезопасной (M20-4): в выгрузке
    оба разрыва — один тег, и различает их только атрибут."""
    spans, problems, _c = engine._parse_docx_anchor_spans(
        make_docx(_break_para("первая", "вторая", kind="page", cid="0")))
    assert problems == []
    assert spans[0]["para_text"] == "первая\fвторая"


def test_an_unmeasured_break_kind_is_refused_not_guessed(engine, make_docx):
    """Разрыв колонки не замерен. Придумать ему символ — сдвинуть все
    смещения после него, поэтому абзац объявляется непрочитанным."""
    spans, problems, _c = engine._parse_docx_anchor_spans(
        make_docx(_break_para("первая", "вторая", kind="column", cid="0")))
    assert any("unsupported elements" in p for p in problems)
    assert not any("\f" in (s.get("para_text") or "") for s in spans)


def test_a_carriage_return_is_refused_too(engine, make_docx):
    """`w:cr` спецификация называет переносом строки, но Google не написал ни
    одного (0 из 8 разрывов, M20-1) — значит не замерено, значит отказ."""
    body = ('<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>первая</w:t></w:r>'
            '<w:r><w:cr/></w:r><w:r><w:t>вторая</w:t></w:r>'
            '<w:commentRangeEnd w:id="0"/></w:p>')
    _spans, problems, _c = engine._parse_docx_anchor_spans(make_docx(body))
    assert any("unsupported elements" in p for p in problems)


def test_a_break_character_cannot_arrive_as_plain_text_at_all(engine,
                                                              make_docx):
    """Почему в парсере нет гвардии на «символ разрыва пришёл текстом»: XML
    таких символов в содержимом не допускает вовсе, и выгрузка с ними —
    битая. Проверяем именно это, а не придуманное поведение."""
    for ch in ("\v", "\f"):
        body = ('<w:p><w:commentRangeStart w:id="0"/>'
                f'<w:r><w:t>первая{ch}вторая</w:t></w:r>'
                '<w:commentRangeEnd w:id="0"/></w:p>')
        spans, problems, _c = engine._parse_docx_anchor_spans(make_docx(body))
        assert spans == []
        assert any("malformed docx export" in p for p in problems)


def test_the_api_side_refuses_a_break_character_as_plain_text(engine):
    """А вот сторона API — это JSON, и там такой символ прийти может. Абзац,
    чей прогон несёт `\f`, прочитался бы как несущий разрыв страницы и совпал
    бы с чужим (codex, круг 2 по плану)."""
    para = _tab([["до\fпосле"]])["body"]["content"][0]["paragraph"]
    text, _pieces, opaque = engine._api_para_read(para)
    assert text is None and opaque == {"textRun(break)"}
    # мягкий перенос в прогоне — это и есть его нормальное место
    soft = _tab([["до\vпосле"]])["body"]["content"][0]["paragraph"]
    assert engine._api_para_read(soft)[0] == "до\vпосле"


def test_offsets_count_the_break_as_one_unit(engine, make_docx):
    """Смещение якоря после разрыва обязано считать его за одну единицу — так
    же, как API (M20-2: и `\\v` в прогоне, и pageBreak шириной 1)."""
    body = ('<w:p><w:r><w:t>до</w:t></w:r><w:r><w:br/></w:r>'
            '<w:commentRangeStart w:id="0"/><w:r><w:t>як</w:t></w:r>'
            '<w:commentRangeEnd w:id="0"/></w:p>')
    spans, problems, _c = engine._parse_docx_anchor_spans(make_docx(body))
    assert problems == []
    assert (spans[0]["start_off"], spans[0]["end_off"]) == (3, 5)


# ---------------------------------------------------------------------------
# сторона API: тот же алфавит, с проверкой ширины
# ---------------------------------------------------------------------------

def test_the_api_side_spells_a_page_break_only_at_width_one(engine):
    para = _tab([["до", {"pageBreak": {}}, "после"]])[
        "body"]["content"][0]["paragraph"]
    text, _pieces, opaque = engine._api_para_read(para)
    assert (text, opaque) == ("до\fпосле", set())

    wide = _tab([["до", {"pageBreak": {}, "_width": 2}, "после"]])[
        "body"]["content"][0]["paragraph"]
    text, _pieces, opaque = engine._api_para_read(wide)
    assert text is None and opaque == {"pageBreak/width"}


def test_a_break_of_the_wrong_width_can_still_hide_an_anchor(engine):
    """«Не смог прочитать» не должно читаться как «не может держать якорь»:
    такой абзац остаётся возможным домом, то есть отказом, а не размещением."""
    tab = _tab([["до", {"pageBreak": {}, "_width": 2}, "после"]])
    paras = engine._api_lattice(tab)["paras"][()]
    _st, _en, text, _pieces, could_host = paras[0]
    assert text is None and could_host is True


def test_a_column_break_leaves_the_paragraph_unreadable(engine):
    para = _tab([["до", {"columnBreak": {}}, "после"]])[
        "body"]["content"][0]["paragraph"]
    text, _pieces, opaque = engine._api_para_read(para)
    assert text is None and opaque == {"columnBreak"}


def test_a_picture_is_skipped_only_where_text_alone_is_compared(engine):
    """Отрезок вкладки сравнивает ТЕКСТ, и картинка в него не добавляет
    ничего; решётка якорей считает СМЕЩЕНИЯ, и там картинка непрозрачна."""
    para = _tab([["до", {"inlineObjectElement": {}}, "после"]])[
        "body"]["content"][0]["paragraph"]
    assert engine._api_para_read(para, textless_ok=True)[0] == "допосле"
    assert engine._api_para_read(para)[0] is None


# ---------------------------------------------------------------------------
# размещение якоря
# ---------------------------------------------------------------------------

def test_a_soft_break_no_longer_closes_the_document(engine, make_docx):
    """Живой случай пост-мортема от 20 августа: один shift+enter в
    прокомментированном абзаце закрывал замены во всём файле."""
    body = _break_para("Пальто и ботинки", "Осенний ассортимент", cid="0")
    spans, problems, _c = engine._parse_docx_anchor_spans(make_docx(body))
    assert problems == []
    ranges, mproblems, ambiguous = engine._map_anchors_to_doc(
        _tab([["Пальто и ботинки\vОсенний ассортимент"]]), spans)
    assert (mproblems, ambiguous) == ([], [])
    assert [(s, e) for s, e, _t, _i in ranges] == [(1, 37)]


def test_a_page_break_paragraph_is_placed_too(engine, make_docx):
    """Абзац с разрывом страницы API отдаёт нечитаемым (M20-3), и раньше он
    давал ноль совпадений. Теперь он читается — обеими сторонами одинаково."""
    body = _break_para("до", "после", kind="page", cid="0")
    spans, _p, _c = engine._parse_docx_anchor_spans(make_docx(body))
    ranges, mproblems, _amb = engine._map_anchors_to_doc(
        _tab([["до", {"pageBreak": {}}, "после"]]), spans)
    assert mproblems == []
    assert [(s, e) for s, e, _t, _i in ranges] == [(1, 9)]


def test_the_twin_trap_is_not_walked_into(engine, make_docx):
    """M20-4 живьём: два абзаца с одинаковым видимым текстом, в одном мягкий
    перенос, в другом разрыв страницы. Якорь стоит на РАЗРЫВЕ СТРАНИЦЫ.

    При нормализации `\\v` -> `\\n` он совпал бы ровно один раз — с чужим
    абзацем, ограда не сработала бы (кандидат один), и тред остался бы без
    защиты, а защищённым — соседний. Здесь он обязан встать на свой."""
    body = (_break_para("ДВОЙНИК", "хвост")
            + _break_para("ДВОЙНИК", "хвост", kind="page", cid="0"))
    spans, _p, _c = engine._parse_docx_anchor_spans(make_docx(body))
    tab = _tab([["ДВОЙНИК\vхвост"],
                ["ДВОЙНИК", {"pageBreak": {}}, "хвост"]])
    ranges, mproblems, ambiguous = engine._map_anchors_to_doc(tab, spans)
    assert (mproblems, ambiguous) == ([], [])
    # второй абзац начинается за первым: 1 + len("ДВОЙНИК\vхвост") + 1
    assert [(s, e) for s, e, _t, _i in ranges] == [(15, 28)]


def test_two_identical_soft_break_paragraphs_are_fenced_not_placed(engine,
                                                                   make_docx):
    """Ограда #26 работает на новом алфавите так же: два дословных двойника —
    кандидаты, а не выбор."""
    body = (_break_para("ПОВТОР", "строка")
            + _break_para("ПОВТОР", "строка", cid="0"))
    spans, _p, _c = engine._parse_docx_anchor_spans(make_docx(body))
    ranges, mproblems, ambiguous = engine._map_anchors_to_doc(
        _tab([["ПОВТОР\vстрока"], ["ПОВТОР\vстрока"]]), spans)
    assert (ranges, mproblems) == ([], [])
    assert len(ambiguous) == 1 and len(ambiguous[0]["candidates"]) == 2


def test_an_opaque_home_is_checked_even_at_one_match(engine, make_docx):
    """Узкое закрытие дыры #30 на новой поверхности. Абзац с разрывом до
    этого релиза не совпадал ни с чем — документ отказывал целиком, — поэтому
    строгость здесь ничего работающего не ломает. Смарт-чип, чьи читаемые
    куски укладываются в этот текст, может быть настоящим домом якоря, и
    поставить якорь на читаемого двойника значило бы оставить живой тред без
    защиты."""
    body = _break_para("ДВОЙНИК", "хвост", cid="0")
    spans, _p, _c = engine._parse_docx_anchor_spans(make_docx(body))
    tab = _tab([["ДВОЙНИК\vхвост"],
                ["ДВОЙНИК\vхвост", {"richLink": {}}]])
    ranges, mproblems, _amb = engine._map_anchors_to_doc(tab, spans)
    assert ranges == []
    assert any("could be its home" in str(p) for p in mproblems)


def test_a_paragraph_without_a_break_keeps_the_old_behaviour(engine,
                                                             make_docx):
    """Обратная сторона предыдущего: документы, которые работают сегодня, не
    трогаются. Тот же смарт-чип рядом с обычным абзацем — якорь ставится, как
    ставился всегда (дыра #30 остаётся открытой сознательно)."""
    spans, _p, _c = engine._parse_docx_anchor_spans(make_docx(_anchored("текст")))
    tab = _tab([["текст"], ["текст", {"richLink": {}}]])
    ranges, mproblems, _amb = engine._map_anchors_to_doc(tab, spans)
    assert mproblems == []
    assert [(s, e) for s, e, _t, _i in ranges] == [(1, 6)]


# ---------------------------------------------------------------------------
# отказ называет причину
# ---------------------------------------------------------------------------

def test_the_refusal_shows_the_difference_instead_of_blaming_ghosts(engine,
                                                                    make_docx):
    """Просьба 2 пост-мортема от 20 августа. Агент два раунда искал призраков,
    которых не было, потому что отказ звал их искать, а разница между двумя
    чтениями абзаца была невидимой."""
    body = _break_para("превью", "вторая строка", kind="column", cid="0")
    spans, problems, _c = engine._parse_docx_anchor_spans(make_docx(body))
    # непрочитанный абзац в теле — это проблема ещё на разборе выгрузки
    assert any("unsupported elements" in p for p in problems)

    # а вот расхождение содержимого доходит до размещения: выгрузка и
    # документ читают ОДИН абзац по-разному, и разница невидима глазом
    spans2, _p2, _c2 = engine._parse_docx_anchor_spans(
        make_docx(_break_para("превью", "вторая строка", cid="0")))
    _r, mproblems, _a = engine._map_anchors_to_doc(
        _tab([["превью\vдругая строка"]]), spans2)
    said = " ".join(str(p) for p in mproblems)
    assert "read differently" in said and "not a ghost" in said
    assert "␋" in said            # управляющие символы видны глазами


def test_the_refusal_names_the_paragraph_that_differs_only_by_a_break(
        engine, make_docx):
    """Следующий шаг после отказа: абзац документа, отличающийся ТОЛЬКО тем,
    как записан разрыв. Здесь выгрузка прочитала разрыв страницы, а документ
    отдаёт мягкий перенос — то есть выгрузка отстала от документа.

    Похожесть по буквам для этого не годится и сюда не поставлена: на
    деливерабле, где четверть абзацев — дословные повторы, «похожий» абзац
    оказался бы чужим двойником, и отказ напечатал бы чужой текст."""
    spans, _p, _c = engine._parse_docx_anchor_spans(
        make_docx(_break_para("ДВОЙНИК", "хвост", kind="page", cid="0")))
    _r, mproblems, _a = engine._map_anchors_to_doc(
        _tab([["ДВОЙНИК\vхвост"]]), spans)
    said = " ".join(str(p) for p in mproblems)
    assert "ДВОЙНИК␌хвост" in said and "ДВОЙНИК␋хвост" in said
    assert "not a ghost" in said


def test_the_refusal_does_not_print_a_paragraph_that_merely_looks_alike(
        engine, make_docx):
    spans, _p, _c = engine._parse_docx_anchor_spans(
        make_docx(_anchored("Заголовок превью")))
    _r, mproblems, _a = engine._map_anchors_to_doc(
        _tab([["Заголовок превьюX"]]), spans)
    said = " ".join(str(p) for p in mproblems)
    assert "Заголовок превьюX" not in said
    assert "no paragraph of the document is close to it" in said


# ---------------------------------------------------------------------------
# доказательство отрезка вкладки (стык с r12)
# ---------------------------------------------------------------------------

def test_the_segment_proof_tells_the_two_breaks_apart(engine):
    """`_same_body_text` — равенство, и это ровно то, ради чего снята
    нормализация: под ней два абзаца ниже были бы одним и тем же."""
    assert engine._same_body_text("до\vпосле", "до\vпосле") is True
    assert engine._same_body_text("до\vпосле", "до\fпосле") is False
    assert engine._same_body_text("до\vпосле", "до\nпосле") is False


# ---------------------------------------------------------------------------
# запись: #40
# ---------------------------------------------------------------------------

def _rewrite_case(engine, text, new_text):
    """Перезапись фрагмента, целиком накрывающего якорь, — путь #21."""
    tab = _tab([[text]])
    start, end = 1, 1 + _u16(text)
    anchors = [(start, end, text, "0")]
    return engine._rewrite_anchor_requests(
        tab, text, new_text, start, end, anchors, {"0": "cid"}, [])


def test_a_soft_break_may_be_written_into_an_anchored_fragment(engine):
    """#40: заголовок и подзаголовок превью — один абзац с мягким переносом.
    Раньше запрет управляющих символов гнал оператора в Docs API руками, и
    там он резал пробел внутри якоря (пост-мортем 19 августа)."""
    got = _rewrite_case(engine, "Заголовок подзаголовок",
                        "Заголовок\vподзаголовок")
    assert got is not None
    requests, _tail = got
    assert [next(iter(r)) for r in requests] == [
        "insertText", "replaceAllText", "deleteContentRange"]
    assert requests[0]["insertText"]["text"] == "Заголовок\vподзаголовок"


def test_the_other_control_characters_stay_forbidden(engine):
    for bad in ("Заголовок\nподзаголовок", "Заголовок\tподзаголовок",
                "Заголовок\fподзаголовок", "Заголовок\x00подзаголовок"):
        assert _rewrite_case(engine, "Заголовок подзаголовок", bad) is None


def test_a_quote_never_crosses_a_page_break(engine):
    """`\\f` — алфавит СРАВНЕНИЯ, не адресации. Правка адресуется цитатой через
    текстовый буфер, а он ставит часового в каждом разрыве индексов, поэтому
    цитата через разрыв страницы не резолвится ни до, ни после #27."""
    tab = _tab([["до", {"pageBreak": {}}, "после"]])
    assert engine._find_quote_in_doctab(tab, "до\fпосле") is None
    assert engine._count_quote_occurrences(tab, "до\fпосле") == 0
    # а через мягкий перенос — резолвится: это обычный символ прогона (M20-5)
    soft = _tab([["до\vпосле"]])
    assert engine._find_quote_in_doctab(soft, "до\vпосле") == (1, 9)


def test_sync_names_the_soft_break_instead_of_calling_it_unsupported(engine):
    """Два пробела на конце строки markdown-it отдаёт как `hardbreak`, и это
    настоящий мягкий перенос, а не «неподдержанная разметка». `sync` такой
    абзац по-прежнему считает неделимым — но заметка теперь называет путь,
    который существует, вместо тупика (#24: отказ обязан называть путь)."""
    els, errors = engine._md_elements("Заголовок  \nПодзаголовок\n")
    assert [e["type"] for e in els] == ["opaque-md"]
    assert any("мягкий перенос" in e and "patch" in e for e in errors)


def _cell_tab(parts, tail="обычный абзац"):
    """documentTab: одна ячейка 1x1 с этими частями, потом абзац тела."""
    inner, at = [], 4
    for part in list(parts) + ["\n"]:
        width = _u16(part) if isinstance(part, str) else part.pop("_width", 1)
        if isinstance(part, str):
            inner.append({"startIndex": at, "endIndex": at + width,
                          "textRun": {"content": part}})
        else:
            inner.append(dict({"startIndex": at, "endIndex": at + width},
                              **part))
        at += width
    cell_end = at + 1
    return {"body": {"content": [
        {"startIndex": 1, "endIndex": cell_end + 1, "table": {
            "tableRows": [{"startIndex": 2, "endIndex": cell_end,
                           "tableCells": [
                               {"startIndex": 3, "endIndex": cell_end,
                                "content": [{"startIndex": 4, "endIndex": at,
                                             "paragraph": {"elements": inner}}]}
                           ]}]}},
        {"startIndex": cell_end + 1, "endIndex": cell_end + 2 + len(tail),
         "paragraph": {"elements": [
             {"startIndex": cell_end + 1, "endIndex": cell_end + 2 + len(tail),
              "textRun": {"content": tail + "\n"}}]}},
    ]}}


def test_an_element_naming_no_kind_at_all_is_refused(engine):
    """Элемент, чей вид читателю неизвестен вовсе, занимает место в индексном
    пространстве. Пропустить его — сдвинуть все смещения после него, и якорь
    получит диапазон, съехавший на эту ширину."""
    para = _tab([["до", {}, "после"]])["body"]["content"][0]["paragraph"]
    text, _pieces, opaque = engine._api_para_read(para)
    assert (text, opaque) == (None, {"unknown"})
    assert engine._api_para_read(para, textless_ok=True)[0] is None


def test_a_break_sharing_its_element_is_not_spelled(engine):
    """Ширина элемента равна одной единице разрыва только тогда, когда в
    элементе больше ничего нет."""
    para = _tab([["до", {"pageBreak": {}, "richLink": {}}, "после"]])[
        "body"]["content"][0]["paragraph"]
    text, _pieces, opaque = engine._api_para_read(para)
    assert text is None and opaque == {"pageBreak", "richLink"}

    # и то же самое, когда сосед у разрыва — прогон текста: читать такой
    # элемент как один текст значит потерять единицу разрыва и сдвинуть все
    # смещения после него
    mixed = {"startIndex": 1, "endIndex": 4,
             "textRun": {"content": "до"}, "pageBreak": {}}
    para2 = {"elements": [mixed,
                          {"startIndex": 4, "endIndex": 10,
                           "textRun": {"content": "после\n"}}]}
    text2, _p2, opaque2 = engine._api_para_read(para2)
    assert text2 is None and opaque2 == {"pageBreak", "textRun"}


def test_a_page_break_in_a_cell_is_read_by_both_sides(engine, make_docx):
    """Ячейка с разрывом страницы раньше была нечитаемой на стороне API —
    `_api_cell_text` возвращал None на любом элементе, кроме прогона."""
    tab = _cell_tab(["до", {"pageBreak": {}}, "после"])
    cell = tab["body"]["content"][0]["table"]["tableRows"][0]["tableCells"][0]
    assert engine._api_cell_text(cell) == "до\fпосле"

    body = ('<w:tbl><w:tr><w:tc><w:p>'
            '<w:commentRangeStart w:id="3"/><w:r><w:t>до</w:t></w:r>'
            '<w:r><w:br w:type="page"/></w:r><w:r><w:t>после</w:t></w:r>'
            '<w:commentRangeEnd w:id="3"/></w:p></w:tc></w:tr></w:tbl>')
    spans, _p, _c = engine._parse_docx_anchor_spans(make_docx(body))
    ranges, mproblems, _amb = engine._map_anchors_to_doc(tab, spans)
    assert mproblems == []
    assert [(s, e) for s, e, _t, _i in ranges] == [(4, 12)]


def test_a_cell_read_differently_by_the_two_sides_is_not_proven(engine,
                                                                make_docx):
    """Обратная сторона: сверка текста ячейки — то единственное, чем ограда
    отличает свою ячейку от одинаковой по форме соседней. Разрыв страницы в
    выгрузке против мягкого переноса в документе — разные ячейки."""
    body = ('<w:tbl><w:tr><w:tc><w:p>'
            '<w:commentRangeStart w:id="3"/><w:r><w:t>до</w:t></w:r>'
            '<w:r><w:br w:type="page"/></w:r><w:r><w:t>после</w:t></w:r>'
            '<w:commentRangeEnd w:id="3"/></w:p></w:tc></w:tr></w:tbl>')
    spans, _p, _c = engine._parse_docx_anchor_spans(make_docx(body))
    ranges, mproblems, _amb = engine._map_anchors_to_doc(
        _cell_tab(["до\vпосле"]), spans)
    assert ranges == []
    assert any("read grid column" in str(p) or "cannot be located" in str(p)
               for p in mproblems)


def test_an_anchor_dragged_across_a_break_keeps_its_coordinates(engine,
                                                                make_docx):
    """Выделение, протянутое из абзаца с мягким переносом в следующий абзац:
    оба конца считаются каждый по своему абзацу, и разрыв в первом обязан
    считаться за одну единицу, иначе конец уедет."""
    body = ('<w:p><w:r><w:t>первая</w:t></w:r><w:r><w:br/></w:r>'
            '<w:commentRangeStart w:id="0"/><w:r><w:t>вторая</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>третья</w:t></w:r>'
            '<w:commentRangeEnd w:id="0"/></w:p>')
    spans, problems, _c = engine._parse_docx_anchor_spans(make_docx(body))
    assert problems == []
    assert spans[0]["anchor_text"] == "вторая\nтретья"
    ranges, mproblems, _amb = engine._map_anchors_to_doc(
        _tab([["первая\vвторая"], ["третья"]]), spans)
    assert mproblems == []
    # «первая\vвторая» = 13 единиц, абзац [1..15); якорь с 8-й до конца «третья»
    assert [(s, e) for s, e, _t, _i in ranges] == [(8, 21)]


def _api_tab(tab_id, title, paragraphs):
    """Вкладка API из списка абзацев (str или список частей)."""
    body = _tab([p if isinstance(p, list) else [p] for p in paragraphs])
    return {"tabProperties": {"tabId": tab_id, "title": title},
            "documentTab": body}


def _docx_head(title):
    return (f'<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr>'
            f'<w:r><w:t>{title}</w:t></w:r></w:p>')


def test_the_tab_segment_proof_reads_a_page_break_too(engine, make_docx):
    """Стык с r12: границы вкладки доказываются сверкой содержимого элемент
    за элементом. Абзац с разрывом страницы раньше был для этой сверки
    нечитаемым — вкладка с ним не доказывалась вовсе."""
    api = [_api_tab("t.0", "Черновик", ["раз", ""]),
           _api_tab("t.втор", "Чистовик",
                    [["две", {"pageBreak": {}}, "страницы"], ""])]
    body = (_docx_head("Черновик") + '<w:p><w:r><w:t>раз</w:t></w:r></w:p>'
            + "<w:p/>" + _docx_head("Чистовик")
            + '<w:p><w:r><w:t>две</w:t></w:r>'
            '<w:r><w:br w:type="page"/></w:r>'
            '<w:r><w:t>страницы</w:t></w:r></w:p>' + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    first, last, why = engine._prove_target_segment(census["outline"], tabs,
                                                    "t.втор")
    assert why is None and (first, last) == (3, 5)


def test_the_tab_proof_still_tells_the_two_breaks_apart(engine, make_docx):
    """И обратное: если в выгрузке мягкий перенос, а в документе разрыв
    страницы, это разные абзацы, и отрезок не доказан. Под нормализацией
    `\\v` -> `\\n` они были бы одинаковыми."""
    api = [_api_tab("t.0", "Черновик", ["раз", ""]),
           _api_tab("t.втор", "Чистовик",
                    [["две", {"pageBreak": {}}, "страницы"], ""])]
    body = (_docx_head("Черновик") + '<w:p><w:r><w:t>раз</w:t></w:r></w:p>'
            + "<w:p/>" + _docx_head("Чистовик")
            + '<w:p><w:r><w:t>две</w:t></w:r><w:r><w:br/></w:r>'
            '<w:r><w:t>страницы</w:t></w:r></w:p>' + "<w:p/>")
    _sp, _pr, census = engine._parse_docx_anchor_spans(make_docx(body))
    tabs = engine._collect_tabs({"tabs": api})
    _f, _l, why = engine._prove_target_segment(census["outline"], tabs,
                                               "t.втор")
    assert why and "текст расходится" in why


# ---------------------------------------------------------------------------
# алфавит текста правки
# ---------------------------------------------------------------------------

def test_an_edit_may_carry_a_soft_break_but_not_a_form_feed(engine):
    """Замерено 23 августа: `insertText` с переводом страницы Docs ПРИНИМАЕТ
    (ответ обычный, успешный) и символ молча выбрасывает. Значит легло не то,
    для чего считались позиции, — и узнать об этом неоткуда. Такой текст
    отклоняется до записи."""
    tab = _tab([["Alpha"]])
    ok = engine._resolve_op(
        {"op": "replace_quote", "quote": "Alpha", "with": "Заголовок\vНиз"},
        tab, None)
    assert ok["text"] == "Заголовок\vНиз"

    with __import__("pytest").raises(SystemExit):
        engine._resolve_op(
            {"op": "insert_after_quote", "quote": "Alpha",
             "text": "\fстраница"}, tab, None)


def test_an_edit_may_still_add_a_paragraph(engine):
    """Обратная сторона: `\\n` в тексте правки — это как автор добавляет абзац,
    и запрещать его нельзя. Проверяется отдельно, потому что соблазн описать
    ограду одним диапазоном «все управляющие» велик."""
    tab = _tab([["Alpha"]])
    got = engine._resolve_op(
        {"op": "insert_after_quote", "quote": "Alpha", "text": "\nНовый абзац"},
        tab, None)
    assert got["text"] == "\nНовый абзац"


def test_an_unreadable_paragraph_between_the_ends_costs_no_coordinates(
        engine, make_docx):
    """Спан, протянутый через несколько абзацев, считает КАЖДЫЙ конец по
    своему абзацу, поэтому непрочитанный абзац посередине координат не
    меняет: он и так целиком внутри якоря и защищён вместе с ним.

    Проверено явно (codex, круг 2 по коду): вопрос был, не проходит ли мимо
    fail-closed незамеренный разрыв в промежуточном абзаце."""
    body = ('<w:p><w:commentRangeStart w:id="0"/>'
            '<w:r><w:t>первый</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>сер</w:t></w:r>'
            '<w:r><w:br w:type="column"/></w:r>'
            '<w:r><w:t>едина</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>третий</w:t></w:r>'
            '<w:commentRangeEnd w:id="0"/></w:p>')
    spans, problems, _c = engine._parse_docx_anchor_spans(make_docx(body))
    assert problems == []
    tab = _tab([["первый"], ["сер", {"columnBreak": {}}, "едина"], ["третий"]])
    ranges, mproblems, _amb = engine._map_anchors_to_doc(tab, spans)
    assert mproblems == []
    # [1..8) первый, [8..18) середина, [18..25) третий
    assert [(s, e) for s, e, _t, _i in ranges] == [(1, 24)]


def test_the_two_alphabets_differ_and_the_refusal_says_so(engine):
    """Обычная правка допускает табуляцию, `\\n` и `\\v`; перезапись
    прокомментированного фрагмента — только `\\v`, потому что считает позиции
    тремя запросами и новый абзац посреди них ломает счёт.

    Расхождение намеренное, но молчащее расхождение — это «одна и та же
    операция проходит здесь и не проходит там» без объяснения, поэтому отказ
    обязан назвать его вслух (codex, круг 3 по коду)."""
    tab = _tab([["Alpha"]])
    anchors = [(1, 6, "Alpha", "0")]
    args = (tab, "Alpha", None, 1, 6, anchors, {"0": "cid"}, [])

    why = engine._why_no_rewrite(*args[:2], "Раз\nДва", *args[3:])
    assert "перевод строки" in why and "мягкий перенос" in why.lower()

    why_tab = engine._why_no_rewrite(*args[:2], "Раз\tДва", *args[3:])
    assert "табуляция" in why_tab

    # а мягкий перенос сюда проходит, и объяснять нечего
    assert engine._rewrite_anchor_requests(
        tab, "Alpha", "Раз\vДва", 1, 6, anchors, {"0": "cid"}, []) is not None
