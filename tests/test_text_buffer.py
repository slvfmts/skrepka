"""UTF-16 index mapping, NUL sentinel, exact-range extraction.

Every case here was found or demanded by a cross-model review round —
these tests pin the empirically verified behavior."""


def _two_para_tab():
    # paragraph 1: "A💡B" (indices 1..5, 💡 is 2 UTF-16 units)
    # paragraph 2 (after a structural gap): "CD" (indices 7..9)
    return {"body": {"content": [
        {"startIndex": 1, "endIndex": 5, "paragraph": {"elements": [
            {"startIndex": 1, "endIndex": 5, "textRun": {"content": "A💡B"}}]}},
        {"startIndex": 7, "endIndex": 9, "paragraph": {"elements": [
            {"startIndex": 7, "endIndex": 9, "textRun": {"content": "CD"}}]}},
    ]}}


def test_non_bmp_find(engine):
    assert engine._find_quote_in_doctab(_two_para_tab(), "💡B") == (2, 5)


def test_quote_never_matches_across_structural_gap(engine):
    assert engine._find_quote_in_doctab(_two_para_tab(), "BCD") is None
    assert engine._count_quote_occurrences(_two_para_tab(), "BCD") == 0


def test_nul_needle_is_unmatchable(engine):
    assert engine._find_quote_in_doctab(_two_para_tab(), "B\x00C") is None
    assert engine._count_quote_occurrences(_two_para_tab(), "B\x00C") == 0


def test_exact_range_roundtrip(engine):
    tab = _two_para_tab()
    assert engine._extract_exact_text_range(tab, 1, 5) == "A💡B"
    assert engine._extract_exact_text_range(tab, 2, 5) == "💡B"


def test_exact_range_fails_closed_on_gap(engine):
    assert engine._extract_exact_text_range(_two_para_tab(), 1, 9) is None


def test_utf16_len(engine):
    assert engine._utf16_len("abc") == 3
    assert engine._utf16_len("💡") == 2
    assert engine._utf16_len("a💡b") == 4


def test_slice_utf16(engine):
    assert engine._slice_utf16("a💡b", 1, 3) == "💡"
    assert engine._slice_utf16("a💡b", 3, 4) == "b"


def test_double_insert_same_index_conflicts(engine):
    assert engine._ranges_overlap(3, 3, 3, 3) is True
    assert engine._ranges_overlap(3, 3, 4, 4) is False
