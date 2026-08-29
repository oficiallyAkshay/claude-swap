"""Tests for ``autoswitch.verify_command``/``autoswitch.verify_timeout_seconds``:
post-activation verification that reverts a switch when the new credential
doesn't actually work.

Every command here shells out to the current Python interpreter
(``sys.executable -c ...``) rather than POSIX-only builtins like ``true``/
``false``, so the suite behaves identically on ubuntu/macOS/Windows.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from unittest.mock import patch

from claude_swap.autoswitch import (
    STATE_FILENAME,
    AutoSwitchEngine,
    QuarantineEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_swap.models import Platform
from claude_swap.settings import AutoSwitchSettings, set_setting
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry

_PY = sys.executable


def _verify_ok() -> str:
    return f'{_PY} -c "import sys; sys.exit(0)"'


def _verify_fail() -> str:
    return f'{_PY} -c "import sys; sys.exit(1)"'


def _verify_sleep(seconds: float) -> str:
    return f'{_PY} -c "import time; time.sleep({seconds})"'


class _Helpers:
    """Mirrors the seed pattern in test_switcher.py's fresh-machine tests."""

    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(
        self, s: ClaudeAccountSwitcher, num: int, email: str,
    ) -> None:
        s._write_account_credentials(
            str(num), email,
            json.dumps({
                "claudeAiOauth": {
                    "accessToken": f"sk-{num}", "refreshToken": f"rt-{num}",
                },
            }),
        )
        s._write_account_config(
            str(num), email,
            json.dumps({
                "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
            }),
        )
        data = s._get_sequence_data() or {
            "activeAccountNumber": None, "lastUpdated": "",
            "sequence": [], "accounts": {},
        }
        data["accounts"][str(num)] = {
            "email": email, "uuid": f"uuid-{num}", "organizationUuid": "",
            "organizationName": "", "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        s._write_json(s.sequence_file, data)

    def _make_live(self, temp_home: Path, s: ClaudeAccountSwitcher, num: int, email: str) -> None:
        """Seed account `num`, then also make it the LIVE identity — lands
        switch_to() in the normal (transaction) path, not fresh-machine, so
        `op["from"]` names a real previous account to restore to."""
        self._seed(s, num, email)
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps({
                "claudeAiOauth": {
                    "accessToken": f"sk-{num}", "refreshToken": f"rt-{num}",
                },
            })
        )
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
        }))


class TestVerifyDisabledByDefault(_Helpers):
    def test_no_verify_key_when_unset(self, temp_home: Path):
        s = self._setup(temp_home)
        self._make_live(temp_home, s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")

        op = s._perform_switch("2", emit_output=False)

        assert "verify" not in op
        assert json.loads(s._read_credentials())["claudeAiOauth"]["refreshToken"] == "rt-2"


class TestVerifySuccess(_Helpers):
    def test_exit_zero_switch_stands(self, temp_home: Path):
        s = self._setup(temp_home)
        self._make_live(temp_home, s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        set_setting(s.backup_dir, "autoswitch.verifyCommand", _verify_ok())

        op = s._perform_switch("2", emit_output=False)

        assert op["verify"] == {"ok": True, "command": _verify_ok()}
        assert op["to"]["number"] == 2
        assert json.loads(s._read_credentials())["claudeAiOauth"]["refreshToken"] == "rt-2"


class TestVerifyFailureRestores(_Helpers):
    def test_exit_nonzero_restores_previous_account(self, temp_home: Path):
        s = self._setup(temp_home)
        self._make_live(temp_home, s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        set_setting(s.backup_dir, "autoswitch.verifyCommand", _verify_fail())

        op = s._perform_switch("2", emit_output=False)

        assert op["verify"]["ok"] is False
        assert "exit 1" in op["verify"]["reason"]
        assert op["verify"]["restoredTo"]["number"] == 1
        assert op["verify"]["attemptedTo"]["number"] == 2
        # Net effect: from == to, nothing really changed.
        assert op["to"] == op["from"]
        assert op["to"]["number"] == 1
        assert json.loads(s._read_credentials())["claudeAiOauth"]["refreshToken"] == "rt-1"
        assert any("Verification failed" in w for w in op["warnings"])

    def test_timeout_restores_previous_account(self, temp_home: Path):
        s = self._setup(temp_home)
        self._make_live(temp_home, s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        set_setting(s.backup_dir, "autoswitch.verifyCommand", _verify_sleep(5))
        set_setting(s.backup_dir, "autoswitch.verifyTimeoutSeconds", "1")

        op = s._perform_switch("2", emit_output=False)

        assert op["verify"]["ok"] is False
        assert "timed out" in op["verify"]["reason"]
        assert op["verify"]["restoredTo"]["number"] == 1
        assert json.loads(s._read_credentials())["claudeAiOauth"]["refreshToken"] == "rt-1"

    def test_json_output_includes_verify_field(self, temp_home: Path):
        s = self._setup(temp_home)
        self._make_live(temp_home, s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        set_setting(s.backup_dir, "autoswitch.verifyCommand", _verify_fail())

        result = s.switch_to("2", json_output=True)

        assert result["switched"] is False
        assert result["reason"] == "verify-failed"
        assert result["verify"]["ok"] is False
        assert result["verify"]["restoredTo"]["number"] == 1

    def test_no_previous_account_stays_active_but_quarantined(self, temp_home: Path):
        """Fresh-machine activation (no prior identity): nothing to restore
        to, so the target stays active — but still gets quarantined."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        set_setting(s.backup_dir, "autoswitch.verifyCommand", _verify_fail())

        op = s._perform_switch("1", emit_output=False)

        assert op["verify"]["ok"] is False
        assert op["verify"]["restoredTo"] is None
        assert op["verify"]["quarantined"] is True
        assert json.loads(s._read_credentials())["claudeAiOauth"]["refreshToken"] == "rt-1"

        state = json.loads((s.backup_dir / STATE_FILENAME).read_text())
        assert state["quarantine"]["1"]["reason"] == "verify-failed"


class TestVerifyAppliesQuarantine(_Helpers):
    def test_failed_target_is_quarantined_in_autoswitch_state(self, temp_home: Path):
        s = self._setup(temp_home)
        self._make_live(temp_home, s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        set_setting(s.backup_dir, "autoswitch.verifyCommand", _verify_fail())

        s._perform_switch("2", emit_output=False)

        state = json.loads((s.backup_dir / STATE_FILENAME).read_text())
        entry = state["quarantine"]["2"]
        assert entry["email"] == "b@example.com"
        assert entry["reason"] == "verify-failed"


class TestRollbackOnlyAfterCompletedWrite(_Helpers):
    """Sibling of the upstream 'a rollback must not undo a move that never
    ran' fix: verify (and any restore it might trigger) must never run for
    a switch whose credential write itself never completed."""

    def test_write_failure_never_invokes_verify(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        s = self._setup(temp_home)
        self._make_live(temp_home, s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        set_setting(s.backup_dir, "autoswitch.verifyCommand", _verify_ok())

        calls: list[str] = []
        monkeypatch.setattr(
            "claude_swap.switcher._run_verify_command",
            lambda *a, **kw: calls.append("called") or (True, "ok"),
        )

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated write failure")

        monkeypatch.setattr(s, "_write_credentials", _boom)

        with pytest.raises(RuntimeError, match="simulated write failure"):
            s._perform_switch("2", emit_output=False)

        assert calls == [], "verify must not run when the activation itself never completed"
        # Account 1 stays the live identity — nothing was ever really switched.
        assert json.loads(s._read_credentials())["claudeAiOauth"]["refreshToken"] == "rt-1"


class TestVerifyThroughAutoEngine(_Helpers):
    """The path most at risk of a self-deadlock: AutoSwitchEngine._perform
    calls switch_to() from inside its own autoswitch_state.json lock, and a
    failed verify needs that exact lock to quarantine the target. Confirms
    the ``_state_lock_held``/``quarantinePending`` handoff actually avoids
    it, end to end through a real engine tick."""

    def test_verify_failure_through_engine_does_not_deadlock_and_quarantines(
        self, temp_home: Path,
    ):
        s = self._setup(temp_home)
        self._make_live(temp_home, s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        # switcher.py's _verify_after_activation re-reads settings.json
        # directly (like every other autoswitch.* knob), independent of the
        # in-memory AutoSwitchSettings the engine itself was built with — so
        # the command must land on disk, not just in `settings` below.
        set_setting(s.backup_dir, "autoswitch.verifyCommand", _verify_fail())
        set_setting(s.backup_dir, "autoswitch.verifyTimeoutSeconds", "5")
        settings = AutoSwitchSettings(
            verify_command=_verify_fail(), verify_timeout_seconds=5.0,
        )
        events: list = []
        engine = AutoSwitchEngine(s, settings, events.append)

        entries = {
            "1": UsageEntry(
                last_good={"five_hour": {"pct": 95.0}, "seven_day": {"pct": 0.0}},
                fetched_at=0.0, age_s=0.0,
            ),
            "2": UsageEntry(
                last_good={"five_hour": {"pct": 10.0}, "seven_day": {"pct": 0.0}},
                fetched_at=0.0, age_s=0.0,
            ),
        }
        with patch.object(s, "usage_entries_by_account", return_value=entries):
            outcome = engine.tick()  # would hang here if the lock deadlocked

        assert outcome is TickOutcome.NO_ACTION

        switch_events = [e for e in events if isinstance(e, SwitchEvent)]
        assert switch_events, "no SwitchEvent emitted for the failed verify"
        assert switch_events[0].verify is not None
        assert switch_events[0].verify["ok"] is False

        quarantine_events = [e for e in events if isinstance(e, QuarantineEvent)]
        assert quarantine_events, "engine never emitted the QuarantineEvent"
        assert quarantine_events[0].number == "2"
        assert quarantine_events[0].reason == "verify-failed"

        # Account 1's credential is restored — it never really left.
        assert json.loads(s._read_credentials())["claudeAiOauth"]["refreshToken"] == "rt-1"

        state = json.loads((s.backup_dir / STATE_FILENAME).read_text())
        assert state["quarantine"]["2"]["reason"] == "verify-failed"
        # The switch never really landed, so it must not be recorded as one.
        assert state.get("lastSwitchTo") != "2"


class TestSkipVerifyOnRestoreCall(_Helpers):
    """The internal restore-switch (verify failed -> switch back) must not
    itself re-run verify — an always-failing command would otherwise
    ping-pong between the two accounts forever."""

    def test_restore_does_not_recurse_into_verify(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        s = self._setup(temp_home)
        self._make_live(temp_home, s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        set_setting(s.backup_dir, "autoswitch.verifyCommand", _verify_fail())

        call_count = {"n": 0}
        from claude_swap import switcher as switcher_module
        real_run = switcher_module._run_verify_command

        def _counting(*args, **kwargs):
            call_count["n"] += 1
            return real_run(*args, **kwargs)

        monkeypatch.setattr(switcher_module, "_run_verify_command", _counting)

        s._perform_switch("2", emit_output=False)

        # Exactly one verify attempt (on the original target) — the restore
        # switch back to account 1 must be verify-free.
        assert call_count["n"] == 1
