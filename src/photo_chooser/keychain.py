"""Ключ Claude API в Keychain через системную утилиту security (SPEC FR-13, NFR-6).

Ключ приходит из вывода security и в аргументы процессов не попадает; при записи его спрашивает сама
security (-w последним аргументом), так что через photo-chooser он не проходит.
"""

import getpass
import re
import subprocess

SERVICE = "anthropic-api-key"  # то же имя записи, что настроено на первом Mac
SECURITY = "/usr/bin/security"
NOT_FOUND = 44  # код security, когда записи нет

_run = subprocess.run  # юнит-тесты подменяют, чтобы не трогать реальный Keychain
_ACCOUNT = re.compile(r'"acct"<blob>="([^"]*)"')


class KeychainError(Exception):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"security завершилась с кодом {code}")


def _find(run, *extra: str) -> subprocess.CompletedProcess:
    return (run or _run)([SECURITY, "find-generic-password", "-s", SERVICE, *extra], capture_output=True, text=True)


def read_api_key(run=None) -> str | None:
    """Ключ из Keychain или None: записи нет, связка заблокирована или доступ отклонён."""
    found = _find(run, "-w")
    return (found.stdout.strip() or None) if found.returncode == 0 else None


def has_api_key(run=None) -> bool:
    """Есть ли запись — без чтения самого ключа."""
    return _find(run).returncode == 0


def key_account(run=None) -> str | None:
    found = _find(run)
    match = _ACCOUNT.search(found.stdout) if found.returncode == 0 else None
    return match.group(1) if match else None


def store_api_key(run=None) -> int:
    """Сохранить или заменить ключ: существующая запись обновляется с тем же аккаунтом. Код выхода security."""
    account = key_account(run) or getpass.getuser()
    return (run or _run)([SECURITY, "add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w"]).returncode


def delete_api_key(run=None) -> bool:
    """True — запись удалена, False — её не было."""
    code = (run or _run)([SECURITY, "delete-generic-password", "-s", SERVICE], capture_output=True,
                         text=True).returncode
    if code not in (0, NOT_FOUND):
        raise KeychainError(code)
    return code == 0
