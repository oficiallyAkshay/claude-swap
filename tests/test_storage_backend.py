"""Tests for the ``storage.backend`` setting: pinning credential storage to
``file`` or ``keychain`` (settings.py + ``CredentialStore`` routing in
credentials.py).

``auto`` (the default) is today's probe-and-fall-back behavior, unchanged.
``file`` must never invoke the ``macos_keychain`` module at all — not even
for best-effort residual cleanup — because pinning it is how a user avoids
Keychain access (and its GUI prompt) altogether. ``keychain`` must never
silently fall back to file storage: a failed op, or running off macOS where
there is no Keychain, raises instead of degrading quietly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import macos_keychain
from claude_swap.credentials import CredentialStore
from claude_swap.exceptions import CredentialError
from claude_swap.models import Platform
from claude_swap.settings import set_setting
from claude_swap.switcher import ClaudeAccountSwitcher


class _Host:
    """Minimal ``_StoreHost``: data only, read at call time (matches the
    pattern in test_credentials.py)."""

    def __init__(self, credentials_dir: Path, *, platform: Platform, storage_backend: str):
        self.platform = platform
        self.credentials_dir = credentials_dir
        self._logger = logging.getLogger("test")
        self.storage_backend = storage_backend


def _poison(name: str):
    def _raise(*args, **kwargs):
        raise AssertionError(f"macos_keychain.{name} must never be called")

    return _raise


def _poison_every_keychain_call(monkeypatch) -> None:
    for name in ("get_password", "set_password", "delete_password", "item_exists"):
        monkeypatch.setattr(macos_keychain, name, _poison(name))


@pytest.fixture
def macos_switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    """Switcher with platform forced to MACOS regardless of host OS, with
    its directories and sequence file ready for seeding."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    switcher._setup_directories()
    switcher._init_sequence_file()
    return switcher


def _seed_account(switcher: ClaudeAccountSwitcher, num: int, email: str, token: str) -> None:
    """Seed one managed account: backup credentials, backup config, and the
    sequence.json record — mirrors the fresh-machine seeding pattern used
    throughout test_switcher.py."""
    switcher._write_account_credentials(
        str(num),
        email,
        json.dumps({
            "claudeAiOauth": {
                "accessToken": f"sk-{token}",
                "refreshToken": f"rt-{token}",
                "expiresAt": 9999999999000,
            },
        }),
    )
    switcher._write_account_config(
        str(num),
        email,
        json.dumps({
            "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
        }),
    )
    data = switcher._get_sequence_data() or {
        "activeAccountNumber": None,
        "lastUpdated": "",
        "sequence": [],
        "accounts": {},
    }
    data["accounts"][str(num)] = {
        "email": email,
        "uuid": f"uuid-{num}",
        "organizationUuid": "",
        "organizationName": "",
        "added": "2024-01-01T00:00:00Z",
    }
    if num not in data["sequence"]:
        data["sequence"].append(num)
        data["sequence"].sort()
    if data["activeAccountNumber"] is None:
        data["activeAccountNumber"] = num
    switcher._write_json(switcher.sequence_file, data)


class TestFileBackendNeverTouchesKeychain:
    """The primary contract of ``storage.backend = "file"``."""

    def test_full_add_switch_switch_back_cycle_never_calls_keychain(
        self, macos_switcher: ClaudeAccountSwitcher, monkeypatch: pytest.MonkeyPatch
    ):
        """Two dummy accounts, pinned to file storage: seed both (the backup
        write half of "add"), activate the first (fresh-machine switch),
        rotate to the second, then rotate back — every credential read/write
        along the way must avoid the Keychain module entirely."""
        set_setting(macos_switcher.backup_dir, "storage.backend", "file")
        _poison_every_keychain_call(monkeypatch)

        _seed_account(macos_switcher, 1, "a@example.com", "1")
        _seed_account(macos_switcher, 2, "b@example.com", "2")

        with patch.object(macos_switcher, "list_accounts"):
            macos_switcher.switch()  # fresh-machine: activates recorded slot 1
        active = json.loads(macos_switcher._read_credentials())
        assert active["claudeAiOauth"]["refreshToken"] == "rt-1"

        with patch.object(macos_switcher, "list_accounts"):
            macos_switcher.switch()  # rotate -> 2
        active = json.loads(macos_switcher._read_credentials())
        assert active["claudeAiOauth"]["refreshToken"] == "rt-2"

        with patch.object(macos_switcher, "list_accounts"):
            macos_switcher.switch()  # rotate back -> 1
        active = json.loads(macos_switcher._read_credentials())
        assert active["claudeAiOauth"]["refreshToken"] == "rt-1"

        # The backups themselves stayed on disk as plain .enc files — never
        # migrated to (or shadowed by) a Keychain item.
        assert macos_switcher._read_account_credentials("1", "a@example.com")
        assert macos_switcher._read_account_credentials("2", "b@example.com")

    def test_write_credentials_never_calls_keychain(
        self, macos_switcher: ClaudeAccountSwitcher, monkeypatch: pytest.MonkeyPatch
    ):
        set_setting(macos_switcher.backup_dir, "storage.backend", "file")
        _poison_every_keychain_call(monkeypatch)

        macos_switcher._store._write_credentials(json.dumps({
            "claudeAiOauth": {"accessToken": "sk-x", "refreshToken": "rt-x"},
        }))

        assert json.loads(macos_switcher._read_credentials())["claudeAiOauth"]["refreshToken"] == "rt-x"

    def test_delete_account_credentials_strict_never_calls_keychain(
        self, macos_switcher: ClaudeAccountSwitcher, monkeypatch: pytest.MonkeyPatch
    ):
        """The transactional strict-delete path (swap/move rollback) still
        must not reach for the Keychain while pinned to file storage."""
        set_setting(macos_switcher.backup_dir, "storage.backend", "file")
        _seed_account(macos_switcher, 1, "a@example.com", "1")
        _poison_every_keychain_call(monkeypatch)

        macos_switcher._store.delete_account_credentials_strict("1", "a@example.com")

        assert macos_switcher._read_account_credentials("1", "a@example.com") == ""


class TestAutoBackendUnchanged:
    """``auto`` (the default, and an explicit "auto") still probes the
    Keychain first, exactly like before this setting existed."""

    def test_default_writes_go_to_the_keychain(self, macos_switcher: ClaudeAccountSwitcher):
        calls = []
        real_set = macos_keychain.set_password

        def _spy(service, account, password, **kwargs):
            calls.append((service, account))
            return real_set(service, account, password, **kwargs)

        with patch.object(macos_keychain, "set_password", _spy):
            macos_switcher._store._write_credentials(json.dumps({
                "claudeAiOauth": {"accessToken": "sk-x", "refreshToken": "rt-x"},
            }))

        assert calls, "auto mode must still try the Keychain first"
        assert macos_switcher._store._last_active_credentials_backend == "keychain"

    def test_explicit_auto_matches_default(self, macos_switcher: ClaudeAccountSwitcher):
        set_setting(macos_switcher.backup_dir, "storage.backend", "auto")
        assert macos_switcher._use_keychain() is True


class TestKeychainBackendNeverFallsBack:
    """``storage.backend = "keychain"``: fail loudly instead of degrading."""

    def test_macos_keychain_failure_raises_instead_of_falling_back_to_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        host = _Host(tmp_path / "backups", platform=Platform.MACOS, storage_backend="keychain")
        store = CredentialStore(host)

        def _boom(service, account, password, **kwargs):
            raise macos_keychain.KeychainError("simulated Keychain failure")

        monkeypatch.setattr(macos_keychain, "set_password", _boom)

        with pytest.raises(CredentialError, match="keychain"):
            store._write_credentials(json.dumps({
                "claudeAiOauth": {"accessToken": "sk-x", "refreshToken": "rt-x"},
            }))

        # No silent fallback: nothing was written to the plaintext file.
        from claude_swap.paths import get_credentials_path
        assert not get_credentials_path().exists()

    def test_non_macos_raises_instead_of_using_file_storage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Off macOS there is no Keychain at all — pinning to it must fail
        loudly on use rather than silently behaving like file storage."""
        host = _Host(tmp_path / "backups", platform=Platform.LINUX, storage_backend="keychain")
        store = CredentialStore(host)

        with pytest.raises(CredentialError, match="keychain"):
            store._write_credentials(json.dumps({
                "claudeAiOauth": {"accessToken": "sk-x", "refreshToken": "rt-x"},
            }))

        from claude_swap.paths import get_credentials_path
        assert not get_credentials_path().exists()

    def test_successful_macos_keychain_write_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The pin only forbids fallback — a healthy Keychain still serves
        every op exactly like auto mode's happy path."""
        host = _Host(tmp_path / "backups", platform=Platform.MACOS, storage_backend="keychain")
        store = CredentialStore(host)

        store._write_credentials(json.dumps({
            "claudeAiOauth": {"accessToken": "sk-x", "refreshToken": "rt-x"},
        }))

        assert store._last_active_credentials_backend == "keychain"
        assert json.loads(store._read_credentials())["claudeAiOauth"]["refreshToken"] == "rt-x"
