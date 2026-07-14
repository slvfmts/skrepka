"""W8 docx anchor-span parser: correct offsets + fail-closed contract.

Includes the two synthetic attack fixtures constructed by the reviewer
model during the W8 code review — both must fail closed."""


def _p(text):
    return f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'


def test_plain_anchor_offsets(engine, make_docx):
    body = ('<w:p><w:r><w:t>До </w:t></w:r>'
            '<w:commentRangeStart w:id="0"/><w:r><w:t>якорь</w:t></w:r>'
            '<w:commentRangeEnd w:id="0"/><w:r><w:t> после</w:t></w:r></w:p>')
    spans, problems = engine._parse_docx_anchor_spans(make_docx(body))
    assert problems == []
    assert len(spans) == 1
    assert spans[0]["anchor_text"] == "якорь"
    assert spans[0]["start_off"] == 3
    assert spans[0]["end_off"] == 8


def test_non_bmp_offsets(engine, make_docx):
    body = ('<w:p><w:r><w:t>a💡</w:t></w:r>'
            '<w:commentRangeStart w:id="1"/><w:r><w:t>x</w:t></w:r>'
            '<w:commentRangeEnd w:id="1"/></w:p>')
    spans, problems = engine._parse_docx_anchor_spans(make_docx(body))
    assert problems == []
    assert spans[0]["start_off"] == 3  # a=1 + 💡=2 UTF-16 units
    assert spans[0]["anchor_text"] == "x"


def test_attack_unsupported_inline_container_fails_closed(engine, make_docx):
    # Reviewer fixture: fldSimple truncates para_text; the truncated "AXC"
    # would exact-match a different paragraph -> must be a problem instead.
    body = ('<w:p><w:commentRangeStart w:id="7"/><w:r><w:t>A</w:t></w:r>'
            '<w:fldSimple w:instr="PAGE"><w:r><w:t>B</w:t></w:r></w:fldSimple>'
            '<w:r><w:t>X</w:t></w:r><w:commentRangeEnd w:id="7"/>'
            '<w:r><w:t>C</w:t></w:r></w:p>' + _p("AXC"))
    spans, problems = engine._parse_docx_anchor_spans(make_docx(body))
    assert any("unsupported elements" in p for p in problems)


def test_attack_marker_hidden_in_tracked_insert_fails_closed(engine, make_docx):
    body = ('<w:p><w:ins w:id="1"><w:commentRangeStart w:id="9"/>'
            '<w:r><w:t>Q</w:t></w:r><w:commentRangeEnd w:id="9"/></w:ins></w:p>')
    spans, problems = engine._parse_docx_anchor_spans(make_docx(body))
    assert any("outside plain body paragraphs" in p for p in problems)


def test_marker_inside_table_fails_closed(engine, make_docx):
    body = ('<w:tbl><w:tr><w:tc><w:p>'
            '<w:commentRangeStart w:id="3"/><w:r><w:t>cell</w:t></w:r>'
            '<w:commentRangeEnd w:id="3"/></w:p></w:tc></w:tr></w:tbl>'
            + _p("обычный абзац"))
    spans, problems = engine._parse_docx_anchor_spans(make_docx(body))
    assert any("outside plain body paragraphs" in p for p in problems)


def test_cross_paragraph_span_fails_closed(engine, make_docx):
    body = ('<w:p><w:commentRangeStart w:id="5"/><w:r><w:t>один</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>два</w:t></w:r><w:commentRangeEnd w:id="5"/></w:p>')
    spans, problems = engine._parse_docx_anchor_spans(make_docx(body))
    assert any("crosses paragraphs" in p for p in problems)


def test_malformed_zip_fails_closed(engine):
    spans, problems = engine._parse_docx_anchor_spans(b"not a zip at all")
    assert spans == []
    assert any("malformed docx" in p for p in problems)


def test_map_anchors_ambiguous_paragraph_fails_closed(engine, make_docx):
    body = ('<w:p><w:commentRangeStart w:id="2"/><w:r><w:t>такой текст</w:t></w:r>'
            '<w:commentRangeEnd w:id="2"/></w:p>')
    spans, problems = engine._parse_docx_anchor_spans(make_docx(body))
    assert problems == []
    # duplicate paragraph text on the API side -> exactly-1 match violated
    tab = {"body": {"content": [
        {"startIndex": 1, "endIndex": 13, "paragraph": {"elements": [
            {"startIndex": 1, "endIndex": 13, "textRun": {"content": "такой текст\n"}}]}},
        {"startIndex": 13, "endIndex": 25, "paragraph": {"elements": [
            {"startIndex": 13, "endIndex": 25, "textRun": {"content": "такой текст\n"}}]}},
    ]}}
    ranges, mproblems = engine._map_anchors_to_doc(tab, spans)
    assert ranges == []
    assert any("matched 2 times" in p for p in mproblems)
