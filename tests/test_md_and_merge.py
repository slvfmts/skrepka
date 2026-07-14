"""Markdown element parsing + three-way merge building blocks."""

import json


def test_md_basic_blocks(engine):
    els, errors = engine._md_elements(
        "# Заголовок\n\nАбзац с **жирным** и [ссылкой](https://e.com).\n\n"
        "* пункт\n* второй с *курсивом*\n")
    assert errors == []
    kinds = [e["type"] for e in els]
    assert kinds == ["h1", "p", "li", "li"]
    sig = json.loads(els[1]["sig"])
    assert ["жирным", ["bold"]] in sig
    assert any(m and m[0].startswith("link:") for _t, m in sig)


def test_md_softbreak_is_opaque(engine):
    els, errors = engine._md_elements("строка один\nстрока два\n")
    assert els[0]["type"] == "opaque-md"
    assert any("soft line break" in e for e in errors)


def test_md_nested_list_is_opaque(engine):
    els, errors = engine._md_elements("* верхний\n  * вложенный\n")
    assert any(e["type"] == "opaque-md" for e in els)
    assert any("nested list" in e for e in errors)


def test_md_table_is_opaque(engine):
    els, _ = engine._md_elements("| a | b |\n| - | - |\n| 1 | 2 |\n")
    assert all(e["type"] == "opaque-md" for e in els if e["type"].startswith("opaque"))


def test_diff_status_shapes(engine):
    st, ins, mp = engine._diff_status(["a", "b", "c", "d"], ["a", "B", "d", "e"])
    assert st == {0: "equal", 1: "changed", 2: "deleted", 3: "equal"}
    assert ins == {4: [3]}
    assert mp[1] == 1 and mp[3] == 2


def test_norm_ws_nbsp(engine):
    assert engine._norm_ws("мало вопросов") == "мало вопросов"


def test_marks_signature_merges_fragmented_runs(engine):
    sig_a = engine._marks_signature([("Пер", ()), ("вый", ()), (" жир", ("bold",))])
    sig_b = engine._marks_signature([("Первый", ()), (" жир", ("bold",))])
    assert sig_a == sig_b


def test_opaque_hash_ignores_indices(engine):
    el_a = {"startIndex": 1, "endIndex": 5,
            "paragraph": {"elements": [{"startIndex": 1, "endIndex": 5,
                                        "textRun": {"content": "x"}}]}}
    el_b = json.loads(json.dumps(el_a))
    el_b["startIndex"], el_b["endIndex"] = 100, 104
    el_b["paragraph"]["elements"][0]["startIndex"] = 100
    el_b["paragraph"]["elements"][0]["endIndex"] = 104
    assert engine._opaque_hash(el_a) == engine._opaque_hash(el_b)


def test_doc_elements_full_state_fingerprint_sees_underline(engine):
    def para(extra_style):
        return {"body": {"content": [{
            "startIndex": 1, "endIndex": 7, "paragraph": {"elements": [
                {"startIndex": 1, "endIndex": 7,
                 "textRun": {"content": "текст\n",
                             "textStyle": extra_style}}]}}]}}
    plain = engine._doc_elements(para({}))
    underlined = engine._doc_elements(para({"underline": True}))
    # alignment identity (type, text) equal, but doc_fp differs
    assert plain[0]["text"] == underlined[0]["text"]
    assert plain[0]["doc_fp"] != underlined[0]["doc_fp"]
    # md-projection signature does NOT see underline (that is the point:
    # detection happens via doc_fp, not via sig)
    assert plain[0]["sig"] == underlined[0]["sig"]


def test_doc_elements_complex_paragraphs_are_opaque(engine):
    tab = {"body": {"content": [
        {"startIndex": 1, "endIndex": 20, "paragraph": {
            "bullet": {"nestingLevel": 1},
            "elements": [{"startIndex": 1, "endIndex": 20,
                          "textRun": {"content": "вложенный пункт\n"}}]}},
        {"startIndex": 20, "endIndex": 40, "paragraph": {"elements": [
            {"startIndex": 20, "endIndex": 40,
             "textRun": {"content": "строка\vс переносом\n"}}]}},
    ]}}
    els = engine._doc_elements(tab)
    assert [e["type"] for e in els] == ["opaque", "opaque"]
    assert all(e["kind"] == "complex-paragraph" for e in els)
