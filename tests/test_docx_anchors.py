"""W8 docx anchor-span parser: correct offsets + fail-closed contract.

Includes the two synthetic attack fixtures constructed by the reviewer
model during the W8 code review — both must fail closed."""


def _p(text):
    return f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'


def test_plain_anchor_offsets(engine, make_docx):
    body = ('<w:p><w:r><w:t>До </w:t></w:r>'
            '<w:commentRangeStart w:id="0"/><w:r><w:t>якорь</w:t></w:r>'
            '<w:commentRangeEnd w:id="0"/><w:r><w:t> после</w:t></w:r></w:p>')
    spans, problems, census = engine._parse_docx_anchor_spans(make_docx(body))
    assert problems == []
    assert len(spans) == 1
    assert spans[0]["anchor_text"] == "якорь"
    assert spans[0]["start_off"] == 3
    assert spans[0]["end_off"] == 8


def test_non_bmp_offsets(engine, make_docx):
    body = ('<w:p><w:r><w:t>a💡</w:t></w:r>'
            '<w:commentRangeStart w:id="1"/><w:r><w:t>x</w:t></w:r>'
            '<w:commentRangeEnd w:id="1"/></w:p>')
    spans, problems, census = engine._parse_docx_anchor_spans(make_docx(body))
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
    spans, problems, census = engine._parse_docx_anchor_spans(make_docx(body))
    assert any("unsupported elements" in p for p in problems)


def test_attack_marker_hidden_in_tracked_insert_fails_closed(engine, make_docx):
    body = ('<w:p><w:ins w:id="1"><w:commentRangeStart w:id="9"/>'
            '<w:r><w:t>Q</w:t></w:r><w:commentRangeEnd w:id="9"/></w:ins></w:p>')
    spans, problems, census = engine._parse_docx_anchor_spans(make_docx(body))
    assert any("outside plain body paragraphs" in p for p in problems)


def test_marker_inside_table_is_reported_as_a_table(engine, make_docx):
    """A comment in a cell is still a problem — but a bounded one: the census
    says the anchor is in a table, and the caller fences off tables instead of
    freezing the document (r8)."""
    body = ('<w:tbl><w:tr><w:tc><w:p>'
            '<w:commentRangeStart w:id="3"/><w:r><w:t>cell</w:t></w:r>'
            '<w:commentRangeEnd w:id="3"/></w:p></w:tc></w:tr></w:tbl>'
            + _p("обычный абзац"))
    spans, problems, census = engine._parse_docx_anchor_spans(make_docx(body))
    assert len(problems) == 1
    assert "inside tables" in problems[0]
    assert problems[0].in_tables == frozenset({"3"})
    assert problems[0].elsewhere == frozenset()
    assert census["in_tables"] == frozenset({"3"})


def test_marker_hidden_outside_a_table_stays_global(engine, make_docx):
    """A tracked-change container is not a table: nothing bounds it, so the
    refusal must stay document-wide."""
    body = ('<w:p><w:ins w:id="1"><w:commentRangeStart w:id="9"/>'
            '<w:r><w:t>Q</w:t></w:r><w:commentRangeEnd w:id="9"/></w:ins></w:p>')
    spans, problems, census = engine._parse_docx_anchor_spans(make_docx(body))
    hidden = [p for p in problems if getattr(p, "elsewhere", None) is not None]
    assert hidden and hidden[0].elsewhere == frozenset({"9"})
    assert hidden[0].in_tables == frozenset()


def test_fence_off_tables_blocks_tables_not_the_document(engine, make_docx):
    body = ('<w:tbl><w:tr><w:tc><w:p>'
            '<w:commentRangeStart w:id="3"/><w:r><w:t>cell</w:t></w:r>'
            '<w:commentRangeEnd w:id="3"/></w:p></w:tc></w:tr></w:tbl>'
            + _p("обычный абзац"))
    _spans, problems, _census = engine._parse_docx_anchor_spans(make_docx(body))
    tab = {"body": {"content": [
        {"startIndex": 1, "endIndex": 40, "table": {"rows": 1}},
        {"startIndex": 40, "endIndex": 55, "paragraph": {"elements": []}},
    ]}}
    remaining, blocked = engine._fence_off_tables(problems, tab)
    assert remaining == []
    assert [(s, e) for s, e, _ in blocked] == [(1, 40)]


def test_fence_off_tables_refuses_when_the_api_shows_no_table(engine, make_docx):
    """The export says «in a table», the API side has none — the two views
    disagree about the document, so nothing may be assumed."""
    body = ('<w:tbl><w:tr><w:tc><w:p>'
            '<w:commentRangeStart w:id="3"/><w:r><w:t>cell</w:t></w:r>'
            '<w:commentRangeEnd w:id="3"/></w:p></w:tc></w:tr></w:tbl>')
    _spans, problems, _census = engine._parse_docx_anchor_spans(make_docx(body))
    remaining, blocked = engine._fence_off_tables(
        problems, {"body": {"content": []}})
    assert remaining == problems
    assert blocked == []


def test_cross_paragraph_span_fails_closed(engine, make_docx):
    body = ('<w:p><w:commentRangeStart w:id="5"/><w:r><w:t>один</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>два</w:t></w:r><w:commentRangeEnd w:id="5"/></w:p>')
    spans, problems, census = engine._parse_docx_anchor_spans(make_docx(body))
    assert any("crosses paragraphs" in p for p in problems)


def test_malformed_zip_fails_closed(engine):
    spans, problems, census = engine._parse_docx_anchor_spans(b"not a zip at all")
    assert spans == []
    assert any("malformed docx" in p for p in problems)


# ---------------------------------------------------------------------------
# #26: identical paragraphs are fenced, not frozen
# ---------------------------------------------------------------------------

def _u16(s):
    return len(s.encode("utf-16-le")) // 2


def _tab(paragraphs):
    """documentTab from a list of paragraphs.

    Each paragraph is a list of parts: a str becomes a textRun, a dict is
    used verbatim as an element ({"person": {}}, {"inlineObjectElement": {}}).
    One index unit per UTF-16 unit, one per non-textRun element — as the API
    counts them — plus the paragraph's own newline.
    """
    content, at = [], 1
    for parts in paragraphs:
        elements, start = [], at
        for part in list(parts) + ["\n"]:
            if isinstance(part, str):
                width = _u16(part)
                elements.append({"startIndex": at, "endIndex": at + width,
                                 "textRun": {"content": part}})
            else:
                width = 1
                elements.append(dict({"startIndex": at, "endIndex": at + 1},
                                     **part))
            at += width
        content.append({"startIndex": start, "endIndex": at,
                        "paragraph": {"elements": elements}})
    return {"body": {"content": content}}


def _anchored(text, cid="2"):
    return (f'<w:p><w:commentRangeStart w:id="{cid}"/>'
            f'<w:r><w:t>{text}</w:t></w:r>'
            f'<w:commentRangeEnd w:id="{cid}"/></w:p>')


def test_identical_paragraphs_are_fenced_not_frozen(engine, make_docx):
    """#26: the anchor is provably in ONE of the copies, so every copy is
    protected and the rest of the document stays editable. Before this, two
    identical paragraphs disabled replaces everywhere."""
    spans, problems, _c = engine._parse_docx_anchor_spans(
        make_docx(_anchored("такой текст") + _p("такой текст")))
    assert problems == []
    tab = _tab([["такой текст"], ["такой текст"]])
    ranges, mproblems, ambiguous = engine._map_anchors_to_doc(tab, spans)
    assert (ranges, mproblems) == ([], [])
    blocked, fproblems = engine._fence_off_ambiguous(ambiguous)
    assert fproblems == []
    assert [(s, e) for s, e, _l in blocked] == [(1, 12), (13, 24)]


def test_fence_names_the_thread_and_the_way_out(engine, make_docx):
    spans, _p1, _c = engine._parse_docx_anchor_spans(
        make_docx(_anchored("текст", cid="7") + _p("текст")))
    _r, _pr, ambiguous = engine._map_anchors_to_doc(
        _tab([["текст"], ["текст"]]), spans)
    blocked, _fp = engine._fence_off_ambiguous(
        ambiguous, attribution={"7": "cmt1"}, file_id="DOC")
    label = blocked[0][2]
    assert "cmt1" in label and "disco=cmt1" in label
    assert "одинаковых копий" in label
    # the way out is the UI, never `update` — the refusal must not teach the
    # destructive path (#24)
    assert "update" not in label


def test_four_anchors_on_a_duplicated_paragraph(engine, make_docx):
    """The live shape of #26: several threads on one paragraph, a clean twin
    elsewhere. Every anchor is fenced in both copies, ranges deduplicated."""
    body = ('<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>аа</w:t></w:r>'
            '<w:commentRangeEnd w:id="0"/><w:r><w:t> </w:t></w:r>'
            '<w:commentRangeStart w:id="1"/><w:r><w:t>бб</w:t></w:r>'
            '<w:commentRangeEnd w:id="1"/></w:p>' + _p("аа бб"))
    spans, problems, _c = engine._parse_docx_anchor_spans(make_docx(body))
    assert problems == []
    _r, mproblems, ambiguous = engine._map_anchors_to_doc(
        _tab([["аа бб"], ["аа бб"]]), spans)
    assert mproblems == []
    blocked, fproblems = engine._fence_off_ambiguous(ambiguous)
    assert fproblems == []
    # аа -> [1,3) and [7,9); бб -> [4,6) and [10,12)
    assert [(s, e) for s, e, _l in blocked] == [
        (1, 3), (4, 6), (7, 9), (10, 12)]


def test_two_threads_on_the_same_range_collapse_to_one_fence(engine, make_docx):
    """Two comments on the same selection export byte-identical ranges. The
    fence must not grow one interval per thread — `_blocked_hits` walks the
    list on every single operation."""
    body = ('<w:p><w:commentRangeStart w:id="0"/><w:commentRangeStart w:id="1"/>'
            '<w:r><w:t>текст</w:t></w:r>'
            '<w:commentRangeEnd w:id="0"/><w:commentRangeEnd w:id="1"/></w:p>'
            + _p("текст"))
    spans, _p1, _c = engine._parse_docx_anchor_spans(make_docx(body))
    _r, _pr, ambiguous = engine._map_anchors_to_doc(
        _tab([["текст"], ["текст"]]), spans)
    blocked, _fp = engine._fence_off_ambiguous(ambiguous)
    assert [(s, e) for s, e, _l in blocked] == [(1, 6), (7, 12)]
    assert "и ещё таких комментариев: 1" in blocked[0][2]


def test_no_match_at_all_still_freezes_the_document(engine, make_docx):
    """Zero candidates means no coordinates at all — there is nothing to
    fence, and the anchor could be anywhere."""
    spans, _p1, _c = engine._parse_docx_anchor_spans(
        make_docx(_anchored("такой текст")))
    ranges, problems, ambiguous = engine._map_anchors_to_doc(
        _tab([["совсем другой"]]), spans)
    assert (ranges, ambiguous) == ([], [])
    assert any("matched 0 times" in p for p in problems)


def test_single_match_maps_exactly_as_before(engine, make_docx):
    spans, _p1, _c = engine._parse_docx_anchor_spans(
        make_docx(_p("До ") + _anchored("якорь")))
    ranges, problems, ambiguous = engine._map_anchors_to_doc(
        _tab([["До "], ["якорь"]]), spans)
    assert (problems, ambiguous) == ([], [])
    assert [(s, e) for s, e, _t, _i in ranges] == [(5, 10)]


def _anchored_link(text, cid="2"):
    """An anchor whose paragraph carries link content — the shape a smart chip
    is believed to export as."""
    return (f'<w:p><w:commentRangeStart w:id="{cid}"/>'
            f'<w:hyperlink><w:r><w:t>{text}</w:t></w:r></w:hyperlink>'
            f'<w:commentRangeEnd w:id="{cid}"/></w:p>')


def test_a_chip_line_does_not_freeze_a_document_it_has_nothing_to_do_with(
        engine, make_docx):
    """A single smart chip on its own line shows NO text, so it "fits" every
    anchor text there is. Treating that as a reason to doubt every anchor
    locked whole documents — and Google's meeting-notes template inserts such
    a line by itself. An anchor whose own paragraph has no link content cannot
    be living inside a chip, so the chip is none of its business."""
    spans, _p1, _c = engine._parse_docx_anchor_spans(
        make_docx(_p("До ") + _anchored("якорь")))
    ranges, problems, ambiguous = engine._map_anchors_to_doc(
        _tab([["До "], ["якорь"], [{"person": {}}]]), spans)
    assert (problems, ambiguous) == ([], [])
    assert [(s, e) for s, e, _t, _i in ranges] == [(5, 10)]


def test_an_unreadable_paragraph_keeps_the_old_refusal(engine, make_docx):
    """A smart chip hides its paragraph's text, so that paragraph could be the
    anchor's real home — and a fence around it would prove nothing, since
    `replaceAllText` acts on the whole tab and reaches inside without ever
    overlapping the operation's range. So the fence is withheld and the
    document refuses exactly as it did before #26. Checked ONLY here, in the
    branch that already refused today: at a single match nothing changes, so
    a stray chip line cannot freeze a document that works."""
    spans, _p1, _c = engine._parse_docx_anchor_spans(
        make_docx(_anchored("Иван") + _p("Иван")))
    tab = _tab([["Иван"], ["Иван"], [{"person": {}}]])
    ranges, problems, ambiguous = engine._map_anchors_to_doc(tab, spans)
    assert (ranges, ambiguous) == ([], [])
    assert any("cannot read" in p for p in problems)


def test_a_chip_line_cannot_freeze_a_document_that_works_today(engine,
                                                               make_docx):
    """The same chip line next to an anchor that maps cleanly: nothing
    changes. A single chip on its own line shows no text at all and therefore
    "fits" every anchor there is — treating that as doubt would lock ordinary
    documents, and Google's meeting-notes template inserts such a line by
    itself (found in review)."""
    spans, _p1, _c = engine._parse_docx_anchor_spans(
        make_docx(_p("До ") + _anchored("якорь")))
    ranges, problems, ambiguous = engine._map_anchors_to_doc(
        _tab([["До "], ["якорь"], [{"person": {}}]]), spans)
    assert (problems, ambiguous) == ([], [])
    assert [(s, e) for s, e, _t, _i in ranges] == [(5, 10)]


def test_an_unknown_element_kind_is_treated_as_unreadable(engine, make_docx):
    """A blocklist, not a whitelist: a kind Google adds tomorrow must fail
    towards refusal. Only kinds with a PROVEN reason to be harmless are
    excluded."""
    spans, _p1, _c = engine._parse_docx_anchor_spans(
        make_docx(_anchored("Иван") + _p("Иван")))
    _r, problems, ambiguous = engine._map_anchors_to_doc(
        _tab([["Иван"], ["Иван"], [{"somethingNewIn2027": {}}]]), spans)
    assert ambiguous == []
    assert any("cannot read" in p for p in problems)


def test_an_image_paragraph_is_not_a_possible_host(engine, make_docx):
    """An anchor inside a paragraph with an inline object already fails closed
    on the export side (`has_objects`), so such a paragraph can never be the
    silent home of an anchor — fencing must not degrade because a document has
    pictures in it.

    The picture ALONE in a paragraph is the case that matters: it shows no
    text at all, so every anchor text fits it vacuously, and only knowing the
    element's kind keeps it out of the host set."""
    spans, _p1, _c = engine._parse_docx_anchor_spans(
        make_docx(_anchored("текст") + _p("текст")))
    tab = _tab([["текст"], ["текст"], [{"inlineObjectElement": {}}],
                [{"inlineObjectElement": {}}, "Рис. 1"]])
    _r, problems, ambiguous = engine._map_anchors_to_doc(tab, spans)
    assert problems == []
    blocked, _fp = engine._fence_off_ambiguous(ambiguous)
    assert [(s, e) for s, e, _l in blocked] == [(1, 6), (7, 12)]


def test_a_chip_paragraph_with_other_text_is_not_a_host(engine, make_docx):
    """The chip hides SOME text, not any text: the fragments the API does show
    must fit inside the anchor paragraph's text, in order."""
    spans, _p1, _c = engine._parse_docx_anchor_spans(
        make_docx(_anchored("текст") + _p("текст")))
    tab = _tab([["текст"], ["текст"],
                ["Совсем другое ", {"person": {}}, " начало"]])
    _r, problems, ambiguous = engine._map_anchors_to_doc(tab, spans)
    assert problems == []
    assert len(ambiguous) == 1


def test_empty_paragraphs_are_never_candidates(engine, make_docx):
    """Every document has plenty of empty paragraphs and they are all equal.
    They stay out of the fence because an empty paragraph is KNOWN text on the
    API side (not None) and an empty anchor is refused earlier — a chain of
    three facts in three functions, which is why it needs a test."""
    spans, _p1, _c = engine._parse_docx_anchor_spans(
        make_docx(_p("") + _anchored("текст") + _p("") + _p("текст") + _p("")))
    _r, problems, ambiguous = engine._map_anchors_to_doc(
        _tab([[], ["текст"], [], ["текст"], []]), spans)
    assert problems == []
    blocked, _fp = engine._fence_off_ambiguous(ambiguous)
    assert [(s, e) for s, e, _l in blocked] == [(2, 7), (9, 14)]


def test_a_candidate_with_unusable_indices_is_refused_not_skipped(engine,
                                                                 make_docx):
    """Silently dropping a candidate is how an anchor ends up outside its own
    fence. The accounting chain treats «this span is protected» as proven."""
    spans, _p1, _c = engine._parse_docx_anchor_spans(
        make_docx(_anchored("текст") + _p("текст")))
    tab = _tab([["текст"], ["текст"]])
    tab["body"]["content"][1]["endIndex"] = None
    _r, problems, ambiguous = engine._map_anchors_to_doc(tab, spans)
    assert any("unusable indices" in p for p in problems)
    assert ambiguous == []


def test_a_span_that_fences_nothing_is_a_problem(engine):
    """Same rule one level down: if no candidate can hold the anchor, the
    fence is empty — and an empty fence would leave the thread naked on a
    document that stays editable."""
    blocked, problems = engine._fence_off_ambiguous([{
        "docx_id": "0", "para_text": "текст", "start_off": 0, "end_off": 5,
        # too short to hold a 5-unit anchor
        "candidates": [(1, 3), (7, 9)],
    }])
    assert blocked == []
    assert any("none of them cannot hold it" in p for p in problems)


def test_one_unfenceable_candidate_is_enough_to_refuse(engine):
    """Not just «all of them»: the anchor may be in the candidate that was
    dropped, and a fence with a hole in it is what the accounting chain
    treats as proof that the thread is protected."""
    blocked, problems = engine._fence_off_ambiguous([{
        "docx_id": "0", "para_text": "текст", "start_off": 0, "end_off": 5,
        "candidates": [(1, 7), (20, 22)],
    }])
    assert blocked == []
    assert any("one of them cannot hold it" in p for p in problems)


def test_a_zero_width_anchor_fences_the_whole_paragraph(engine):
    """Both overlap checks are strict, so an empty interval could never fire."""
    blocked, problems = engine._fence_off_ambiguous([{
        "docx_id": "0", "para_text": "текст", "start_off": 2, "end_off": 2,
        "candidates": [(1, 7), (7, 13)],
    }])
    assert problems == []
    assert [(s, e) for s, e, _l in blocked] == [(1, 7), (7, 13)]


def test_the_fence_stops_before_the_paragraph_terminator(engine):
    """`en - 1` holds the paragraph's own newline: visible text ends before
    it, so an anchor reaching `en` is not an anchor we read correctly. The
    whole-paragraph fallback is the only thing allowed that far."""
    ok, problems = engine._fence_off_ambiguous([{
        "docx_id": "0", "para_text": "текст", "start_off": 0, "end_off": 5,
        "candidates": [(1, 7), (7, 13)],          # ce == en - 1 exactly
    }])
    assert problems == []
    assert [(s, e) for s, e, _l in ok] == [(1, 6), (7, 12)]

    over, problems = engine._fence_off_ambiguous([{
        "docx_id": "0", "para_text": "текст", "start_off": 0, "end_off": 6,
        "candidates": [(1, 7), (7, 13)],          # ce == en, one too far
    }])
    assert over == []
    assert any("cannot hold it" in p for p in problems)


def test_the_refusal_names_a_remedy_that_fits_the_reason(engine):
    """#24 in miniature. «Разберитесь с призраками» is right for an
    unaccounted thread and plain wrong for a duplicated paragraph — and a
    refusal naming a path that does not exist is what cost 18 threads."""
    dup = engine._anchor_map_remedy(
        "anchor 0 matches 2 paragraphs and one of them cannot hold it")
    assert "Различите копии" in dup and "призрак" not in dup

    chip = engine._anchor_map_remedy(
        "anchor 0 matches 2 paragraphs, and a paragraph whose text skrepka "
        "cannot read (a smart chip) could be its home too")
    assert "чип" in chip and "Различите копии" not in chip

    ghost = engine._anchor_map_remedy("anchor span 7 has no comments.xml entry")
    assert "призрак" in ghost
