"""Ключ Claude API в Keychain (SPEC FR-13, NFR-6): вызовы /usr/bin/security заменены заглушкой."""

import getpass
import subprocess

import pytest

from photo_chooser import keychain
from photo_chooser.keychain import SECURITY, SERVICE, KeychainError

FOUND_ATTRS = ('keychain: "/Users/v/Library/Keychains/login.keychain-db"\nclass: "genp"\nattributes:\n'
               '    "acct"<blob>="valerii"\n    "svce"<blob>="anthropic-api-key"\n')


class FakeSecurity:
    """Подменяет subprocess.run: отвечает кодами по порядку вызовов и записывает аргументы."""

    def __init__(self, *responses: tuple[int, str]):
        self._responses = list(responses)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        code, stdout = self._responses.pop(0)
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr="")


def test_read_returns_key_from_stdout():
    run = FakeSecurity((0, "sk-test\n"))
    assert keychain.read_api_key(run) == "sk-test"
    assert run.calls[0][0] == [SECURITY, "find-generic-password", "-s", SERVICE, "-w"]


@pytest.mark.parametrize("code", [44, 51, 128])
def test_read_missing_or_denied_is_none(code):
    # 44 — записи нет; 51 и 128 — пользователь отклонил окно доступа или связка заблокирована
    assert keychain.read_api_key(FakeSecurity((code, ""))) is None


def test_read_empty_stdout_is_none():
    assert keychain.read_api_key(FakeSecurity((0, "\n"))) is None


def test_has_key_does_not_read_secret():
    run = FakeSecurity((0, FOUND_ATTRS), (44, ""))
    assert keychain.has_api_key(run) is True
    assert keychain.has_api_key(run) is False
    assert all("-w" not in args for args, _ in run.calls)


def test_store_reuses_existing_account():
    run = FakeSecurity((0, FOUND_ATTRS), (0, ""))
    assert keychain.store_api_key(run) == 0
    args, kwargs = run.calls[1]
    assert args == [SECURITY, "add-generic-password", "-U", "-s", SERVICE, "-a", "valerii", "-w"]
    assert "capture_output" not in kwargs  # ключ вводится с терминала в саму security


def test_store_new_account_is_login_name():
    run = FakeSecurity((44, ""), (0, ""))
    keychain.store_api_key(run)
    args, _ = run.calls[1]
    assert args[args.index("-a") + 1] == getpass.getuser()
    assert args[-1] == "-w"


def test_store_returns_security_exit_code():
    assert keychain.store_api_key(FakeSecurity((44, ""), (45, ""))) == 45


def test_store_never_passes_key_in_args():
    run = FakeSecurity((0, FOUND_ATTRS), (0, ""))
    keychain.store_api_key(run)
    assert not any(a.startswith("sk-") for args, _ in run.calls for a in args)


def test_delete_found_missing_failed():
    assert keychain.delete_api_key(FakeSecurity((0, ""))) is True
    assert keychain.delete_api_key(FakeSecurity((44, ""))) is False
    with pytest.raises(KeychainError, match="security завершилась с кодом 1"):
        keychain.delete_api_key(FakeSecurity((1, "")))


def test_unit_tests_cannot_call_security():
    with pytest.raises(AssertionError, match="/usr/bin/security"):
        keychain.read_api_key()
