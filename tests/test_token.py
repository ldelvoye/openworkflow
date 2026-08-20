"""What a pasted token has to look like before it is stored, and what a
rejection is allowed to say about it.
"""

import pytest

from smorg.auth.token import InvalidToken, accepted_token, credentials_from_token

SECRET = "github_pat_11ABCDEFG0abcdefghijklmnop"


def test_a_token_is_taken_as_entered():
    assert accepted_token(SECRET) == SECRET


def test_surrounding_whitespace_is_dropped():
    assert accepted_token(f"  {SECRET}\n") == SECRET


@pytest.mark.parametrize(
    "entered",
    [
        "",
        "   ",
        "\n",
    ],
)
def test_an_empty_entry_is_refused(entered):
    with pytest.raises(InvalidToken, match="no token entered"):
        accepted_token(entered)


def test_a_token_broken_across_a_line_is_refused():
    """A paste that wrapped is the common way to store a token that can never
    work; the service would only reject it a refresh later."""
    with pytest.raises(InvalidToken, match="whitespace"):
        accepted_token("github_pat_11ABC\nDEFG0abcdefghijklmnop")


def test_a_token_carrying_a_control_character_is_refused():
    with pytest.raises(InvalidToken):
        accepted_token("github_pat_\x1b[31m11ABCDEFG")


@pytest.mark.parametrize(
    "entered",
    [
        f"{SECRET} {SECRET}",
        f"github_pat_\n{SECRET}",
        "\x00" + SECRET,
    ],
)
def test_a_rejection_never_repeats_the_token(entered):
    """The whole point of the store is that tokens do not leak; an error
    message printed to a terminal is exactly where one would."""
    with pytest.raises(InvalidToken) as raised:
        accepted_token(entered)
    assert SECRET not in str(raised.value)
    assert "github_pat" not in str(raised.value)


def test_stored_credentials_carry_no_expiry_and_nothing_to_refresh():
    """Claiming an expiry nobody told us would have the refresh layer act on a
    guess, and a refresh token that does not exist would have it try."""
    credentials = credentials_from_token(SECRET)

    assert credentials.access_token == SECRET
    assert credentials.refresh_token is None
    assert credentials.expires_at is None


def test_credentials_still_redact_a_pasted_token():
    assert SECRET not in repr(credentials_from_token(SECRET))
