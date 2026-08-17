from oflow.core.text import capped, printable, printable_block


def test_printable_block_keeps_newlines_and_strips_escapes():
    hostile = "line one\n\x1b[31mline two\x1b[0m\nline three"
    assert printable_block(hostile) == "line one\n[31mline two[0m\nline three"


def test_printable_block_caps_total_length():
    assert len(printable_block("x" * 5000)) == 4000


def test_printable_block_of_empty_is_empty():
    assert printable_block("") == ""


def test_printable_block_with_no_limit_is_uncapped():
    assert len(printable_block("x" * 5000, limit=None)) == 5000


def test_printable_still_flattens_everything():
    assert "\n" not in printable("a\nb")


def test_capped_leaves_under_limit_text_unchanged():
    assert capped("short", limit=100) == "short"


def test_capped_at_exactly_the_limit_is_unchanged():
    assert capped("x" * 5, limit=5) == "x" * 5


def test_capped_marks_a_cut_with_a_visible_trailing_marker():
    result = capped("x" * 10, limit=5)
    assert result == "xxxxx\n\n… (truncated)"
