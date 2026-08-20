from smorg.core.text import sanitize_block, sanitize_line, truncate


def test_sanitize_block_keeps_newlines_and_strips_escapes():
    hostile = "line one\n\x1b[31mline two\x1b[0m\nline three"
    assert sanitize_block(hostile) == "line one\n[31mline two[0m\nline three"


def test_sanitize_block_caps_total_length():
    assert len(sanitize_block("x" * 5000)) == 4000


def test_sanitize_block_of_empty_is_empty():
    assert sanitize_block("") == ""


def test_sanitize_block_with_no_limit_is_untruncate():
    assert len(sanitize_block("x" * 5000, limit=None)) == 5000


def test_sanitize_line_still_flattens_everything():
    assert "\n" not in sanitize_line("a\nb")


def test_truncate_leaves_under_limit_text_unchanged():
    assert truncate("short", limit=100) == "short"


def test_truncate_at_exactly_the_limit_is_unchanged():
    assert truncate("x" * 5, limit=5) == "x" * 5


def test_truncate_marks_a_cut_with_a_visible_trailing_marker():
    result = truncate("x" * 10, limit=5)
    assert result == "xxxxx\n\n… (truncated)"
