"""Эмодзи-реакция на тексте — не комментарий (пост-мортем Точки 12.10, M33).

Google кладёт реакцию в comments.xml записью без `w:author` с текстом
«1 реакция» / «Имя – 🎇 | дата», со своими маркерами в document.xml, а в
`comments.list` её нет. Её ключ без автора попадал в «неизвестные API» и
запирал все замены документа на месяц.
"""
import io
import zipfile

WORDML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_REACTION = (
    '<w:comment w:id="15" w:date="2026-09-08T12:08:03Z">'
    '<w:p><w:r><w:t xml:space="preserve">1 реакция</w:t></w:r></w:p>'
    '<w:p><w:r><w:t xml:space="preserve">Светлана Дюмаева – 🎇 | '
    '08.09.2026, 12:08 UTC</w:t></w:r></w:p></w:comment>')
_THREAD = (
    '<w:comment w:id="3" w:author="Светлана Дюмаева" '
    'w:date="2026-09-08T11:32:11Z">'
    '<w:p><w:r><w:t>каких?</w:t></w:r></w:p></w:comment>')


def _docx(comments, body):
    document = (f'<?xml version="1.0"?><w:document xmlns:w="{WORDML}">'
                f"<w:body>{body}</w:body></w:document>")
    cxml = (f'<?xml version="1.0"?><w:comments xmlns:w="{WORDML}">'
            f"{comments}</w:comments>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", document)
        z.writestr("word/comments.xml", cxml)
    return buf.getvalue()


def _anchored(cid, text):
    return (f'<w:commentRangeStart w:id="{cid}"/>'
            f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'
            f'<w:commentRangeEnd w:id="{cid}"/>'
            f'<w:r><w:commentReference w:id="{cid}"/></w:r>')


BODY = (f"<w:p><w:r><w:t xml:space=\"preserve\">до </w:t></w:r>"
        f"{_anchored('3', 'пяти мест.')}</w:p>"
        f"<w:p><w:r><w:t xml:space=\"preserve\">В итоге </w:t></w:r>"
        f"{_anchored('15', 'получилось без кредитов.')}</w:p>")

THREAD_API = {"id": "c3", "createdTime": "2026-09-08T11:32:11.866Z",
              "author": {"displayName": "Светлана Дюмаева"},
              "quotedFileContent": {"value": "пяти мест."},
              "resolved": False, "content": "каких?", "replies": []}


def test_a_reaction_is_neither_a_record_nor_a_span(engine):
    blob = _docx(_THREAD + _REACTION, BODY)
    records, rproblems = engine._docx_comment_records(blob)
    spans, sproblems, census = engine._parse_docx_anchor_spans(blob)
    assert rproblems == [] and sproblems == []
    assert [r["docx_id"] for r in records] == ["3"]
    assert [s["docx_id"] for s in spans] == ["3"]
    assert "15" not in census["in_body"]


def test_a_reaction_does_not_freeze_the_document(engine):
    """Связка целиком, как на пути записи: учёт не видит ни неизвестной
    записи, ни якоря без записи — ни глобального стопа, ни ограды."""
    blob = _docx(_THREAD + _REACTION, BODY)
    records, _ = engine._docx_comment_records(blob)
    spans, _, census = engine._parse_docx_anchor_spans(blob)
    problems, metrics = engine._account_anchored_comments(
        [THREAD_API], records, spans,
        universe=engine._key_owners_universe([THREAD_API]),
        marker_census=census)
    assert problems == []
    assert metrics["docx_comment_entries"] == 1


def test_a_comment_without_author_but_with_words_is_still_a_record(engine):
    """Признак узкий: без автора, но с обычным текстом — не реакция. Такая
    запись по-прежнему разбирается учётом (и огораживается как неизвестная),
    а не выбрасывается молча."""
    odd = ('<w:comment w:id="15" w:date="2026-09-08T12:08:03Z">'
           '<w:p><w:r><w:t>а это что?</w:t></w:r></w:p></w:comment>')
    blob = _docx(_THREAD + odd, BODY)
    records, _ = engine._docx_comment_records(blob)
    spans, _, _census = engine._parse_docx_anchor_spans(blob)
    assert sorted(r["docx_id"] for r in records) == ["15", "3"]
    assert sorted(s["docx_id"] for s in spans) == ["15", "3"]


def test_an_authored_record_shaped_like_a_reaction_is_kept(engine):
    """Человек может написать в комментарии ровно «1 реакция / X – Y | Z».
    С автором это комментарий, а не реакция."""
    shaped = _REACTION.replace('w:id="15"',
                               'w:id="15" w:author="Иван"')
    blob = _docx(_THREAD + shaped, BODY)
    records, _ = engine._docx_comment_records(blob)
    assert sorted(r["docx_id"] for r in records) == ["15", "3"]


def test_several_reactors_are_one_reaction(engine):
    many = _REACTION.replace(
        "1 реакция", "2 реакции").replace(
        "</w:comment>",
        '<w:p><w:r><w:t>Ivan Petrov – 👍 | 09.09.2026, 10:00 UTC</w:t>'
        '</w:r></w:p></w:comment>')
    blob = _docx(_THREAD + many, BODY)
    records, _ = engine._docx_comment_records(blob)
    assert [r["docx_id"] for r in records] == ["3"]
