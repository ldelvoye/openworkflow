from oflow.core.text import printable, printable_block


def test_printable_block_keeps_newlines_and_strips_escapes():
    hostile = "line one\n\x1b[31mline two\x1b[0m\nline three"
    assert printable_block(hostile) == "line one\n[31mline two[0m\nline three"


def test_printable_block_caps_total_length():
    assert len(printable_block("x" * 5000)) == 4000


def test_printable_block_of_empty_is_empty():
    assert printable_block("") == ""


def test_printable_still_flattens_everything():
    assert "\n" not in printable("a\nb")
