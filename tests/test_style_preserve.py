"""Style preservation across sync rewrites (paragraphStyle + textStyle).

Empirical background (live-verified 2026-07-14):
- a bare deleteContentRange+insertText rewrite KEEPS paragraphStyle (the \n
  paragraph mark is untouched);
- our own updateParagraphStyle with fields=namedStyleType RESETS every other
  paragraph property to the named style's defaults — that was the destroyer;
- deleteParagraphBullets rewrites indentStart/indentFirstLine, so for plain
  blocks it must run BEFORE the paragraphStyle update;
- inserted text does NOT reliably inherit the old runs' textStyle, so the
  textStyle outcome of a rewrite is made deterministic: uniform originals
  are restored, non-uniform ones are cleared to defaults (and reported).
"""

import json

FONT10 = {"fontSize": {"magnitude": 10, "unit": "PT"}}
RED = {"foregroundColor": {"color": {"rgbColor": {"red": 1}}}}
LINKED = {"underline": True, "link": {"url": "https://old.example"},
          "foregroundColor": {"color": {"rgbColor": {"blue": 1}}}}


def run(length, style):
    return {"len": length, "style": style}


# --- _visible_run_styles ---

def test_visible_runs_strip_trailing_newline(engine):
    runs = engine._visible_run_styles([("текст\n", FONT10)])
    assert runs == [run(5, FONT10)]


def test_visible_runs_drop_newline_only_run(engine):
    # Google can split the paragraph mark into its own default-styled run;
    # it must not participate in uniformity (codex style-r1 #3)
    runs = engine._visible_run_styles([("текст", FONT10), ("\n", {})])
    assert runs == [run(5, FONT10)]


def test_visible_runs_utf16_lengths(engine):
    runs = engine._visible_run_styles([("а𝕏б\n", {})])
    assert runs == [run(4, {})]  # surrogate pair counts as 2


# --- _uniform_text_style ---

def test_uniform_all_default_runs(engine):
    style, ok = engine._uniform_text_style([{}, {}])
    assert ok and style == {}


def test_uniform_same_font_size(engine):
    style, ok = engine._uniform_text_style([FONT10, dict(FONT10)])
    assert ok and style == FONT10


def test_uniform_ignores_bold_and_link(engine):
    # bold/italic/link are md-driven and must not break uniformity
    style, ok = engine._uniform_text_style(
        [dict(FONT10, bold=True), dict(FONT10, link={"url": "https://e.com"})])
    assert ok and style == FONT10


def test_non_uniform_color(engine):
    style, ok = engine._uniform_text_style([FONT10, dict(FONT10, **RED)])
    assert not ok and style is None


def test_uniform_empty_paragraph(engine):
    style, ok = engine._uniform_text_style([])
    assert ok and style == {}


# --- _capture_preserve ---

def _rel(para_style=None, run_styles=None, kind="p"):
    return {"type": kind, "text": "т", "para_style": para_style or {},
            "run_styles": run_styles or []}


def test_capture_style_only_carries_run_spans_and_type(engine):
    spans = [run(3, FONT10), run(2, RED)]
    preserve, dropped = engine._capture_preserve(
        _rel({"alignment": "END"}, spans), with_text=False)
    assert preserve["para_style"] == {"alignment": "END"}
    assert preserve["type"] == "p"
    assert preserve["run_spans"] == spans
    assert "text_style" not in preserve
    assert not dropped


def test_capture_rewrite_uniform(engine):
    preserve, dropped = engine._capture_preserve(
        _rel(run_styles=[run(5, FONT10)], kind="li"), with_text=True)
    assert preserve["text_style"] == FONT10
    assert preserve["type"] == "li"
    assert "run_spans" not in preserve
    assert not dropped


def test_capture_rewrite_split_terminal_newline_still_uniform(engine, doc_tab):
    # regression for codex style-r1 #3, end to end through _doc_elements
    tab = doc_tab([(1, 6, "текст", {"textStyle": FONT10}),
                   (6, 7, "\n", {})])
    els = engine._doc_elements(tab)
    preserve, dropped = engine._capture_preserve(els[0], with_text=True)
    assert not dropped
    assert preserve["text_style"] == FONT10


def test_capture_rewrite_non_uniform_clears_and_reports(engine):
    # non-uniform originals must CLEAR deterministically, not leave the
    # rewritten text with whatever styling it inherited (codex style-r1 #2)
    preserve, dropped = engine._capture_preserve(
        _rel(run_styles=[run(3, FONT10), run(2, dict(FONT10, **RED))]),
        with_text=True)
    assert dropped
    assert preserve["text_style"] == {}


# --- _style_requests_for_block with preserve ---

def _block(kind="p", text="новый текст", sig=None):
    return {"type": kind, "text": text,
            "sig": sig or json.dumps([[text, []]], ensure_ascii=False)}


def _kinds(reqs):
    return [next(iter(r)) for r in reqs]


def _para_req(reqs):
    return next(r["updateParagraphStyle"] for r in reqs
                if "updateParagraphStyle" in r)


def _text_reqs(reqs):
    return [r["updateTextStyle"] for r in reqs if "updateTextStyle" in r]


def _full_mask(engine):
    return ",".join(engine._PRESERVE_TEXT_FIELDS)


def test_plain_block_bullets_removed_before_paragraph_style(engine):
    reqs = engine._style_requests_for_block(_block(), 10, preserve={
        "type": "p",
        "para_style": {"namedStyleType": "NORMAL_TEXT",
                       "indentStart": {"magnitude": 36, "unit": "PT"}}})
    kinds = _kinds(reqs)
    assert kinds.index("deleteParagraphBullets") < \
        kinds.index("updateParagraphStyle")


def test_li_block_bullets_created_after_paragraph_style(engine):
    reqs = engine._style_requests_for_block(
        _block("li", "пункт"), 10, preserve=None)
    kinds = _kinds(reqs)
    assert kinds.index("updateParagraphStyle") < \
        kinds.index("createParagraphBullets")


def test_preserved_para_fields_ride_with_named_style(engine):
    reqs = engine._style_requests_for_block(_block(), 10, preserve={
        "type": "p",
        "para_style": {"namedStyleType": "NORMAL_TEXT", "alignment": "END",
                       "lineSpacing": 150,
                       "headingId": "h.readonly",  # must NOT be copied
                       "shading": {"backgroundColor": {}}}})
    upd = _para_req(reqs)
    fields = upd["fields"].split(",")
    assert "namedStyleType" in fields
    assert {"alignment", "lineSpacing", "shading"} <= set(fields)
    assert "headingId" not in fields
    assert upd["paragraphStyle"]["alignment"] == "END"
    assert upd["paragraphStyle"]["namedStyleType"] == "NORMAL_TEXT"


def test_type_change_clears_para_and_text(engine):
    # p -> h2 restructure: clean named style AND cleared text styling —
    # the old uniform style must NOT ride along (codex style-r1 #1), but a
    # full-mask clear MUST still be emitted (inherited styling is arbitrary)
    reqs = engine._style_requests_for_block(_block("h2"), 10, preserve={
        "type": "p",
        "para_style": {"namedStyleType": "NORMAL_TEXT", "alignment": "END"},
        "text_style": dict(FONT10)})
    upd = _para_req(reqs)
    assert upd["fields"] == "namedStyleType"
    assert upd["paragraphStyle"] == {"namedStyleType": "HEADING_2"}
    restore = [t for t in _text_reqs(reqs)
               if set(t["fields"].split(",")) ==
               set(engine._PRESERVE_TEXT_FIELDS)]
    assert len(restore) == 1
    assert restore[0]["textStyle"] == {}


def test_p_to_li_is_a_restructure(engine):
    # p and li share NORMAL_TEXT — the structural type must decide
    # (codex style-r2 #1): converting an indented paragraph to a list item
    # must NOT drag the paragraph indents along
    reqs = engine._style_requests_for_block(
        _block("li", "пункт"), 10, preserve={
            "type": "p",
            "para_style": {"namedStyleType": "NORMAL_TEXT",
                           "indentStart": {"magnitude": 36, "unit": "PT"}},
            "text_style": dict(FONT10)})
    upd = _para_req(reqs)
    assert upd["fields"] == "namedStyleType"
    assert "indentStart" not in upd["paragraphStyle"]
    restore = [t for t in _text_reqs(reqs)
               if t["fields"] == _full_mask(engine)]
    assert restore and restore[0]["textStyle"] == {}


def test_li_to_p_is_a_restructure(engine):
    # the reverse direction must not restore bullet-era indentation either
    reqs = engine._style_requests_for_block(_block(), 10, preserve={
        "type": "li",
        "para_style": {"namedStyleType": "NORMAL_TEXT",
                       "indentStart": {"magnitude": 18, "unit": "PT"},
                       "indentFirstLine": {"magnitude": 0, "unit": "PT"}},
        "text_style": {}})
    upd = _para_req(reqs)
    assert upd["fields"] == "namedStyleType"
    assert "indentStart" not in upd["paragraphStyle"]


def test_no_preserve_matches_legacy_shape(engine):
    reqs = engine._style_requests_for_block(_block(), 10)
    upd = _para_req(reqs)
    assert upd["fields"] == "namedStyleType"
    text_reqs = _text_reqs(reqs)
    assert [t["fields"] for t in text_reqs] == ["link", "bold,italic"]


def test_reset_clears_link_in_dedicated_empty_request(engine):
    # live-verified 2026-07-14: {"link": None} is silently dropped from the
    # body by the google client, and a MIXED request ({"bold": False},
    # fields="bold,italic,link") clears bold/italic but LEAVES the link.
    # Only a dedicated textStyle={} + fields="link" request removes it.
    link_reset, bi_reset = _text_reqs(
        engine._style_requests_for_block(_block(), 10))[:2]
    assert link_reset["fields"] == "link"
    assert link_reset["textStyle"] == {}
    assert bi_reset["fields"] == "bold,italic"
    assert "link" not in bi_reset["textStyle"]


def test_fresh_block_preserve_clears_inherited_styling(engine):
    # inserted blocks get _FRESH_BLOCK_PRESERVE: text inserted next to
    # styled text inherits its look and must be cleared (codex style-r2 #2)
    reqs = engine._style_requests_for_block(
        _block(text="вставка"), 10, preserve=engine._FRESH_BLOCK_PRESERVE)
    upd = _para_req(reqs)
    assert upd["fields"] == "namedStyleType"  # nothing preserved
    clears = [t for t in _text_reqs(reqs)
              if t["fields"] == _full_mask(engine)]
    assert len(clears) == 1
    assert clears[0]["textStyle"] == {}


def test_uniform_text_style_reapplied_with_full_mask(engine):
    text = "новый текст"
    reqs = engine._style_requests_for_block(_block(text=text), 10, preserve={
        "type": "p", "para_style": {}, "text_style": dict(FONT10)})
    text_reqs = _text_reqs(reqs)
    assert len(text_reqs) == 3
    _link_reset, reset, restore = text_reqs
    assert reset["fields"] == "bold,italic"
    # FULL mask: absent fields must be cleared (inserted text can inherit
    # stray styling from a neighbour), values from the captured style
    assert set(restore["fields"].split(",")) == \
        set(engine._PRESERVE_TEXT_FIELDS)
    assert restore["textStyle"] == FONT10
    end = 10 + engine._utf16_len(text)
    assert restore["range"] == {"startIndex": 10, "endIndex": end}


def test_preserve_restore_precedes_md_spans(engine):
    # a link span must land AFTER the uniform restore so the automatic link
    # color/underline wins inside the span
    sig = json.dumps([["сло", []], ["во", ["link:https://e.com"]]],
                     ensure_ascii=False)
    reqs = engine._style_requests_for_block(
        _block(text="слово", sig=sig), 10,
        preserve={"type": "p", "para_style": {},
                  "text_style": {"underline": False}})
    fields = [t["fields"] for t in _text_reqs(reqs)]
    # the LAST "link" request is the md span (the first is the link reset)
    md_link_at = len(fields) - 1 - fields[::-1].index("link")
    assert fields.index(_full_mask(engine)) < md_link_at


# --- style_only: per-run restore with link topology (codex style-r2 #4) ---

def test_style_only_run_spans_restored_after_md_spans(engine):
    # style-only block (text untouched): per-run styles are reapplied AFTER
    # the md spans so re-linking/reset side effects cannot destroy a custom
    # link look or a colored word (codex style-r1 #4)
    text = "слово"
    sig = json.dumps([["сло", ["bold"]], ["во", []]], ensure_ascii=False)
    reqs = engine._style_requests_for_block(
        _block(text=text, sig=sig), 10,
        preserve={"type": "p", "para_style": {},
                  "run_spans": [run(3, RED), run(2, {})]})
    text_reqs = _text_reqs(reqs)
    full = _full_mask(engine)
    # order: link reset -> bold/italic reset -> bold span -> per-run restores
    assert [t["fields"] for t in text_reqs] == \
        ["link", "bold,italic", "bold", full, full]
    r1, r2 = text_reqs[3], text_reqs[4]
    assert r1["range"] == {"startIndex": 10, "endIndex": 13}
    assert r1["textStyle"] == RED
    assert r2["range"] == {"startIndex": 13, "endIndex": 15}
    assert r2["textStyle"] == {}  # full mask clears back to defaults


def test_style_only_restore_filters_non_preserve_fields(engine):
    reqs = engine._style_requests_for_block(
        _block(text="аб"), 10,
        preserve={"type": "p", "para_style": {},
                  "run_spans": [run(2, dict(RED, bold=True))]})
    restore = _text_reqs(reqs)[-1]
    assert restore["textStyle"] == RED  # bold stays md-driven


def test_style_only_removed_link_clears_ghost_appearance(engine):
    # the link was removed in md: the old blue/underline captured from the
    # linked era must NOT be restored (codex style-r2 #4), and the link
    # itself needs a TARGETED unset — the whole-block link reset is
    # silently ignored over mixed ranges (API quirk, live-verified)
    reqs = engine._style_requests_for_block(
        _block(text="было"), 10,
        preserve={"type": "p", "para_style": {},
                  "run_spans": [run(4, dict(LINKED))]})
    link_clear, restore = _text_reqs(reqs)[-2:]
    assert link_clear["fields"] == "link"
    assert link_clear["textStyle"] == {}
    assert link_clear["range"] == {"startIndex": 10, "endIndex": 14}
    assert restore["fields"] == _full_mask(engine)
    assert restore["textStyle"] == {}  # cleared, not blue/underlined
    assert restore["range"] == link_clear["range"]


def test_style_only_removed_link_in_mixed_block(engine):
    # the API quirk case proper: a MIXED linked/plain block — the targeted
    # unset must cover exactly the old linked run, never the plain runs
    text = "до ссылка после"
    reqs = engine._style_requests_for_block(
        _block(text=text), 10,
        preserve={"type": "p", "para_style": {},
                  "run_spans": [run(3, {}), run(6, dict(LINKED)),
                                run(6, {})]})
    unsets = [t for t in _text_reqs(reqs) if t["fields"] == "link"
              and t["range"] != {"startIndex": 10, "endIndex": 25}]
    assert unsets == [{"range": {"startIndex": 13, "endIndex": 19},
                       "textStyle": {}, "fields": "link"}]


def test_style_only_new_link_keeps_default_look(engine):
    # a link ADDED in md над plain text: no restore over that piece, the
    # automatic default link styling must stand (codex style-r2 #4)
    text = "слово тут"
    sig = json.dumps([["слово", ["link:https://new.example"]], [" тут", []]],
                     ensure_ascii=False)
    reqs = engine._style_requests_for_block(
        _block(text=text, sig=sig), 10,
        preserve={"type": "p", "para_style": {},
                  "run_spans": [run(9, RED)]})
    full = _full_mask(engine)
    restores = [t for t in _text_reqs(reqs) if t["fields"] == full]
    # only the non-link piece [15, 19) is restored
    assert len(restores) == 1
    assert restores[0]["range"] == {"startIndex": 15, "endIndex": 19}
    assert restores[0]["textStyle"] == RED


def test_style_only_unchanged_link_keeps_custom_look(engine):
    # link present on both sides: the captured custom appearance survives
    text = "ссылка"
    sig = json.dumps([["ссылка", ["link:https://old.example"]]],
                     ensure_ascii=False)
    reqs = engine._style_requests_for_block(
        _block(text=text, sig=sig), 10,
        preserve={"type": "p", "para_style": {},
                  "run_spans": [run(6, dict(LINKED, underline=False))]})
    restore = _text_reqs(reqs)[-1]
    assert restore["range"] == {"startIndex": 10, "endIndex": 16}
    assert restore["textStyle"]["underline"] is False
    assert "foregroundColor" in restore["textStyle"]


def test_style_only_mismatched_spans_skip_restore(engine, capsys):
    # defensive: captured runs must tile the block exactly or the restore
    # is skipped (never style wrong ranges)
    reqs = engine._style_requests_for_block(
        _block(text="длинный текст"), 10,
        preserve={"type": "p", "para_style": {}, "run_spans": [run(2, RED)]})
    full = _full_mask(engine)
    assert all(t["fields"] != full for t in _text_reqs(reqs))
    assert "style restore skipped" in capsys.readouterr().err


def test_split_by_intervals_partial_overlap(engine):
    pieces = list(engine._split_by_intervals(
        5, 12, [(0, 7, True), (7, 10, False), (10, 20, True)]))
    assert pieces == [(5, 7, True), (7, 10, False), (10, 12, True)]


def test_split_by_intervals_uncovered_tail_defaults_false(engine):
    pieces = list(engine._split_by_intervals(0, 10, [(2, 4, True)]))
    assert pieces == [(0, 2, False), (2, 4, True), (4, 10, False)]


# --- raw styles: exposed on _doc_elements, kept OUT of the sidecar ---

def test_doc_elements_carry_raw_styles(engine, doc_tab):
    tab = doc_tab([(1, 7, "текст\n", {"textStyle": FONT10})])
    tab["body"]["content"][0]["paragraph"]["paragraphStyle"] = {
        "namedStyleType": "NORMAL_TEXT", "alignment": "END"}
    els = engine._doc_elements(tab)
    assert els[0]["para_style"]["alignment"] == "END"
    assert els[0]["run_styles"] == [run(5, FONT10)]


def test_sidecar_entries_exclude_raw_styles(engine, doc_tab):
    doc = {"revisionId": "r1",
           "tabs": [{"tabProperties": {"tabId": "t0", "index": 0},
                     "documentTab": doc_tab([(1, 7, "текст\n")])}]}
    payload = engine._sidecar_payload("doc-id", "/tmp/x.md", "текст\n", doc)
    assert payload["sync_supported"], payload["reason"]
    for entry in payload["elements"]:
        assert "para_style" not in entry
        assert "run_styles" not in entry
        assert "start" not in entry
