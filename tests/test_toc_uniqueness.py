"""Issue #23: table-of-contents text is part of replaceAllText safety.

The live behaviour is deliberately treated conservatively: TOC text is a
possible replaceAllText match, but it is not made into a user-addressable
range.  That removes the document-wide TOC refusal without allowing an
unmeasured write into generated content.
"""


def _paragraph(text, start):
    end = start + len(text) + 1
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {"elements": [{
            "startIndex": start,
            "endIndex": end,
            "textRun": {"content": text + "\n", "textStyle": {}},
        }]},
    }


def _tab(body_text, *toc_texts):
    body = _paragraph(body_text, 1)
    content = [body]
    if toc_texts:
        toc_content = []
        start = body["endIndex"]
        for text in toc_texts:
            toc_content.append(_paragraph(text, start))
            start = toc_content[-1]["endIndex"]
        content.append({
            "startIndex": toc_content[0]["startIndex"],
            "endIndex": toc_content[-1]["endIndex"],
            "tableOfContents": {"content": toc_content},
        })
    return {"body": {"content": content}}


def _rewrite(engine, doc_tab, text="Старый текст", new="Новая фраза"):
    start = 1
    end = start + engine._utf16_len(text)
    return engine._rewrite_anchor_requests(
        doc_tab=doc_tab,
        search_text=text,
        new_text=new,
        start=start,
        end=end,
        anchors=[(start, end, text, "0")],
        attribution={"0": "c1"},
        named_intervals=[],
    )


def test_toc_duplicate_counts_as_a_possible_replace_all_match(engine):
    tab = _tab("Одинаковый заголовок", "Одинаковый заголовок")

    assert engine._count_quote_occurrences(tab, "Одинаковый заголовок") == 1
    assert engine._count_text_in_table_of_contents(
        tab, "Одинаковый заголовок") == 1
    assert engine._replace_all_match_count(tab, "Одинаковый заголовок") == 2


def test_toc_walker_counts_text_nested_in_a_table(engine):
    tab = _tab("Обычный текст")
    tab["body"]["content"].append({
        "tableOfContents": {"content": [{
            "table": {"tableRows": [{"tableCells": [{
                "content": [_paragraph("Заголовок в ячейке", 100)],
            }]}]},
        }]},
    })

    assert engine._count_text_in_table_of_contents(
        tab, "Заголовок в ячейке") == 1


def test_unrelated_toc_no_longer_blocks_safe_anchor_rewrite(engine):
    result = _rewrite(engine, _tab("Старый текст", "Другой заголовок"))

    assert result is not None
    requests, _tail_len = result
    assert [next(iter(request)) for request in requests] == [
        "insertText", "replaceAllText", "deleteContentRange"]


def test_projected_rewrite_match_in_toc_is_refused(engine):
    text = "Старый текст"
    new = "Новая фраза"
    projected_needle = text[:-1] + new
    tab = _tab(text, projected_needle)

    assert _rewrite(engine, tab, text=text, new=new) is None


def test_toc_text_is_not_silently_made_a_patch_target(engine):
    tab = _tab("Тело", "Только в оглавлении")

    assert engine._find_quote_in_doctab(tab, "Только в оглавлении") is None
    assert engine._replace_all_match_count(tab, "Только в оглавлении") == 1
