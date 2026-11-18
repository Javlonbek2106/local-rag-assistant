"""Roadmap M4.1a, decision D-3: the one-time pairing code."""
import hmac
import re

import pytest

from rag import config
from rag.auth import pairing
from rag.auth.pairing import PairingBoard
from tests.auth_support import Clock


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def shown():
    return []


@pytest.fixture
def board(clock, shown):
    return PairingBoard(clock=clock, show=shown.append)


def code_of(shown):
    return re.search(r"(\d{4}) (\d{4})", shown[-1]).expand(r"\1\2")


def test_a_code_is_eight_digits_shown_once_and_exchanges_once(board, shown):
    assert board.request() == "ok" and len(shown) == 1
    code = code_of(shown)
    assert re.fullmatch(r"\d{8}", code)
    assert board.exchange(code) == "ok"
    assert board.exchange(code) == "invalid"  # used


def test_the_code_comes_from_the_secrets_module_not_from_random(board, shown, monkeypatch):
    drawn = []
    monkeypatch.setattr(pairing.secrets, "choice", lambda alphabet: drawn.append(alphabet) or "7")
    board.request()
    assert len(drawn) == config.PAIRING_DIGITS and code_of(shown) == "77777777"


def test_the_code_leaves_only_through_the_console(clock, capsys, caplog):
    board = PairingBoard(clock=clock)  # the real way of showing: the console of the application
    with caplog.at_level("DEBUG"):
        board.request()
    out, err = capsys.readouterr()
    code = re.search(r"(\d{4}) (\d{4})", out).expand(r"\1\2")
    assert err == "" and caplog.records == []  # not the logging system, not stderr
    assert code not in repr(vars(board)) and code not in str(board)  # the board keeps a digest, not the code


def test_the_waiting_code_is_compared_in_constant_time(board, shown, monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(pairing.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    board.request()
    board.exchange(code_of(shown))
    assert calls


def test_a_code_expires(board, shown, clock):
    board.request()
    clock.advance(config.PAIRING_TTL_S)
    assert board.exchange(code_of(shown)) == "invalid"


def test_a_code_is_still_good_just_before_it_expires(board, shown, clock):
    board.request()
    clock.advance(config.PAIRING_TTL_S - 1)
    assert board.exchange(code_of(shown)) == "ok"


@pytest.mark.parametrize("typed", ["", "1234567", "123456789", "abcdefgh", "1234 567x", "１２３４５６７８",
                                   "12345678\n9"])
def test_a_malformed_code_is_invalid(board, shown, typed):
    board.request()
    assert board.exchange(typed) == "invalid"


def test_the_code_may_be_typed_with_the_space_or_a_hyphen(board, shown):
    board.request()
    code = code_of(shown)
    assert board.exchange(f"{code[:4]}-{code[4:]}") == "ok"


def test_nothing_waiting_is_the_same_word_as_wrong_or_expired(board):
    assert board.exchange("12345678") == "invalid"


def test_a_wrong_guess_uses_up_the_waiting_code_so_a_guess_is_one_try_per_code(board, shown):
    board.request()
    code = code_of(shown)
    wrong = "00000000" if code != "00000000" else "11111111"
    assert board.exchange(wrong) == "invalid"
    assert board.exchange(code) == "invalid"  # the right one is gone too


def test_a_new_request_cannot_come_too_soon_and_changes_nothing(board, shown, clock):
    board.request()
    first = code_of(shown)
    clock.advance(config.PAIRING_REQUEST_GAP_S - 1)
    assert board.request() == "rate_limited" and len(shown) == 1  # nothing shown
    assert board.exchange(first) == "ok"  # and the first code still works


def test_a_new_request_replaces_the_old_code(board, shown, clock, monkeypatch):
    digits = iter("11111111" + "22222222")  # two different codes, so the test cannot be fooled by a lucky draw
    monkeypatch.setattr(pairing.secrets, "choice", lambda alphabet: next(digits))
    board.request()
    clock.advance(config.PAIRING_REQUEST_GAP_S)
    assert board.request() == "ok"
    assert board.exchange("11111111") == "invalid"  # the first one was replaced (and this try uses the second up)
    clock.advance(config.PAIRING_REQUEST_GAP_S)
    digits = iter("33333333")
    monkeypatch.setattr(pairing.secrets, "choice", lambda alphabet: next(digits))
    board.request()
    assert board.exchange("33333333") == "ok"


def test_too_many_attempts_start_a_lockout_that_destroys_the_waiting_code(board, shown, clock):
    board.request()
    code = code_of(shown)
    for _ in range(config.PAIRING_ATTEMPTS):
        assert board.exchange("not a code") == "invalid"
    assert board.exchange(code) == "rate_limited"  # even the right one is not looked at now
    clock.advance(config.PAIRING_REQUEST_GAP_S)
    assert board.request() == "rate_limited"  # no new code during the lockout
    assert board.retry_after() > 0
    clock.advance(config.PAIRING_LOCKOUT_S)
    assert board.request() == "ok"
    assert board.exchange(code_of(shown)) == "ok"  # normal again after the lockout
    assert board.exchange(code) == "invalid"  # the old code never came back


def test_the_attempt_limit_is_per_window_not_forever(board, shown, clock):
    for _ in range(config.PAIRING_ATTEMPTS - 1):
        board.exchange("nope")
    clock.advance(config.PAIRING_WINDOW_S)
    for _ in range(config.PAIRING_ATTEMPTS):
        assert board.exchange("nope") == "invalid"  # the old attempts have left the window


def test_a_step_up_code_cannot_be_used_for_a_login(board, shown):
    board.request(purpose="step-up", session="S1")
    assert board.exchange(code_of(shown), purpose="login") == "invalid"


def test_a_step_up_code_cannot_be_used_by_another_session(board, shown, clock):
    board.request(purpose="step-up", session="S1")
    assert board.exchange(code_of(shown), purpose="step-up", session="S2") == "invalid"
    clock.advance(config.PAIRING_REQUEST_GAP_S)
    board.request(purpose="step-up", session="S1")
    assert board.exchange(code_of(shown), purpose="step-up", session="S1") == "ok"


def test_a_login_code_cannot_confirm_a_step_up(board, shown):
    board.request(purpose="login")
    assert board.exchange(code_of(shown), purpose="step-up", session="S1") == "invalid"


def test_a_console_that_cannot_be_written_to_leaves_no_code_waiting_and_no_gap(clock):
    def broken(_text):
        raise ValueError("I/O operation on closed file")

    board = PairingBoard(clock=clock, show=broken)
    assert board.request() == "unavailable"
    assert board.exchange("12345678") == "invalid"  # nothing was made, so nothing can be guessed
    shown = []
    board._show = shown.append
    assert board.request() == "ok"  # no waiting period was started by the failure
    assert board.exchange(code_of(shown)) == "ok"
