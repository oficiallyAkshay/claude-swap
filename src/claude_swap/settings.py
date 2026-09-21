"""Tool settings persisted at ``<backup_root>/settings.json``.

One versioned JSON file for user-tunable claude-swap preferences, written
atomically with the backup dir's 0600/0700 modes. v1 carries the
``autoswitch`` and ``ui`` sections; other sections can be added additively.
Unknown keys (future fields, other tools' experiments) survive a round trip.

Reading is forgiving — a missing or corrupt file yields defaults with a logged
warning, never a crash — so a bad hand edit degrades to default behavior.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from claude_swap.exceptions import ConfigError
from claude_swap.fsutil import replace_with_retry

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"

_logger = logging.getLogger("claude-swap")


@dataclass(frozen=True)
class AutoSwitchSettings:
    """Policy knobs for the auto-switch engine (``cswap auto``).

    ``threshold`` is binding-window utilization (max of the 5h/7d percentages):
    at or above it the engine looks for a better account. 90 rather than 95
    leaves margin for the macOS ~30s Keychain pickup tail and for heavy
    subagent turns burning past the mark before a swap lands. A proactive
    candidate must itself sit below the threshold (never land somewhere that
    re-triggers next tick); on ``strategy: "best"`` it then qualifies outright
    once it strictly beats the active account's headroom — equal headroom
    holds — with no added percentage-point margin (``hysteresis_pct`` no
    longer gates this decision; owner ruling, 2026-09-21).
    """

    threshold: float = 90.0
    interval_seconds: float = 60.0
    cooldown_seconds: float = 300.0
    # Accepted for settings.json backward compatibility only — an existing
    # hand-edited or persisted config with this key must still load. No
    # longer read by the `best`-strategy ranking gate in
    # `autoswitch._rank_candidates` (owner ruling, 2026-09-21): that branch
    # now takes any below-threshold candidate with strictly more headroom
    # than the active account, margin-free. Kept in case a future strategy
    # or gate wants a percentage-point margin again.
    hysteresis_pct: float = 10.0
    strategy: str = "best"  # "best" (most headroom) or "consume-first" (soonest weekly reset)
    include_api_key_accounts: bool = False
    unhealthy_ticks: int = 3
    # Comma-separated model display name(s) (e.g. "Fable" or "Fable,Opus"),
    # or "all" for every scoped window an account reports. Each named model's
    # per-model weekly limit is folded into the binding window, so the engine
    # switches off an account whose model quota is exhausted even while its
    # 5h/7d windows still have headroom. None = account-wide 5h/7d only
    # (default).
    model: str | None = None
    # A shell command run after ANY account activation (the auto engine and
    # manual `cswap switch`/`switch --to`) to confirm the new credential
    # actually works. None (default) = disabled, zero behavior change: no
    # command runs and every switch stands exactly as before this setting
    # existed. When set, exit 0 means the switch stands; a non-zero exit or
    # a timeout (verify_timeout_seconds) restores the previously-active
    # account and quarantines the target so the engine doesn't immediately
    # re-pick it (autoswitch.py's existing quarantine machinery).
    verify_command: str | None = None
    # Bound on verify_command's runtime; a hung command must not hang the
    # switch forever. Only meaningful when verify_command is set.
    verify_timeout_seconds: float = 10.0


@dataclass(frozen=True)
class UiSettings:
    """Appearance preferences (``ui`` section). ``theme`` selects the TUI/CLI
    color theme; ``auto`` follows terminal-background detection."""

    theme: str = "auto"


@dataclass(frozen=True)
class StorageSettings:
    """Credential storage backend selection (``storage`` section).

    ``backend`` picks how ``CredentialStore`` routes the active credential and
    per-account backup ops:

    - ``auto`` (default): today's behavior, unchanged — probe the macOS
      Keychain and fall back to file storage on failure, sticky for the rest
      of the process with a cooldown re-probe (``credentials.py``'s
      ``_use_keychain``/``_kc_call``).
    - ``file``: pin file storage; the Keychain module is never invoked, on
      any platform.
    - ``keychain``: pin the Keychain; never fall back. A failed op — or
      running off macOS, where there is no Keychain to use — raises instead
      of silently degrading to file storage.
    """

    backend: str = "auto"


_SECTION_DEFAULT_SOURCES = {
    "autoswitch": AutoSwitchSettings,
    "ui": UiSettings,
    "storage": StorageSettings,
}


@dataclass(frozen=True)
class SettingSpec:
    """Metadata for one user-tunable settings.json key.

    Single source of truth for bounds/choices: both the lenient clamp on load
    (`_clamped`) and the strict validation in `cswap config set`
    (`parse_setting_value`) read from here, so the two can't drift.
    """

    section: str  # top-level JSON section ("autoswitch", "ui")
    json_key: str  # camelCase key inside the section
    field: str  # snake_case AutoSwitchSettings field
    kind: str  # "float" | "int" | "bool" | "choice"
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.json_key}"

    @property
    def default(self):
        return getattr(_SECTION_DEFAULT_SOURCES[self.section](), self.field)


# settings.json uses camelCase (matching the repo's other JSON artifacts);
# dataclass fields stay snake_case.
SETTING_SPECS: dict[str, SettingSpec] = {
    spec.dotted: spec
    for spec in (
        SettingSpec(
            "autoswitch", "threshold", "threshold", "float", 50.0, 99.9,
            help="Switch when the binding 5h/7d window reaches this pct",
        ),
        SettingSpec(
            "autoswitch", "intervalSeconds", "interval_seconds", "float", 15.0, 3600.0,
            help="Poll interval for the cswap auto loop, in seconds",
        ),
        SettingSpec(
            "autoswitch", "cooldownSeconds", "cooldown_seconds", "float", 0.0, 86400.0,
            help="Minimum seconds between proactive switches",
        ),
        SettingSpec(
            "autoswitch", "hysteresisPct", "hysteresis_pct", "float", 0.0, 50.0,
            help="No longer gates the 'best' strategy's ranking (kept for old configs)",
        ),
        SettingSpec(
            "autoswitch", "strategy", "strategy", "choice",
            choices=("best", "consume-first"),
            help="How auto-switch picks the target account",
        ),
        SettingSpec(
            "autoswitch", "includeApiKeyAccounts", "include_api_key_accounts", "bool",
            help="Allow rotating onto managed API-key accounts (bill per token)",
        ),
        SettingSpec(
            "autoswitch", "unhealthyTicks", "unhealthy_ticks", "int", 1, 100,
            help="Consecutive failed polls before an account is unhealthy",
        ),
        SettingSpec(
            "autoswitch", "model", "model", "string",
            help="Also switch on these models' weekly limits (e.g. Fable, Fable,Opus, or all)",
        ),
        SettingSpec(
            "autoswitch", "verifyCommand", "verify_command", "string",
            help="Shell command run after activation; non-zero/timeout reverts the switch",
        ),
        SettingSpec(
            "autoswitch", "verifyTimeoutSeconds", "verify_timeout_seconds", "float", 1.0, 300.0,
            help="Bound on verifyCommand's runtime, in seconds",
        ),
        SettingSpec(
            "ui", "theme", "theme", "choice", choices=("dark", "light", "auto"),
            help="Color theme; auto follows the terminal background",
        ),
        SettingSpec(
            "storage", "backend", "backend", "choice",
            choices=("auto", "file", "keychain"),
            help="Credential storage: auto-probe, pin file, or pin Keychain",
        ),
    )
}

_AUTOSWITCH_KEYS: dict[str, str] = {
    spec.field: spec.json_key
    for spec in SETTING_SPECS.values()
    if spec.section == "autoswitch"
}


def settings_path(backup_root: Path) -> Path:
    return backup_root / SETTINGS_FILENAME


def parse_model_names(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated model list, trimmed and case-insensitively
    deduped (first spelling wins). Shared by the auto engine and the manual
    switch strategies so both read ``autoswitch.model`` identically."""
    if not value:
        return ()
    seen: dict[str, str] = {}
    for part in value.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return tuple(seen.values())


def _clamped(settings: AutoSwitchSettings) -> AutoSwitchSettings:
    """Clamp values into the SETTING_SPECS ranges; bad types → the default."""

    def num(value, default: float, lo: float, hi: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(min(max(value, lo), hi))

    kwargs = {}
    for spec in SETTING_SPECS.values():
        if spec.section != "autoswitch":
            continue
        value = getattr(settings, spec.field)
        if spec.kind in ("float", "int"):
            clamped = num(value, spec.default, spec.lo, spec.hi)
            kwargs[spec.field] = int(clamped) if spec.kind == "int" else clamped
        elif spec.kind == "bool":
            kwargs[spec.field] = bool(value)
        elif spec.kind == "string":
            # A non-empty string keeps as-is; anything else reverts to default
            # (None) so a null/garbage settings.json value disables the filter.
            kwargs[spec.field] = value if isinstance(value, str) and value else spec.default
        else:  # choice
            if value not in spec.choices:
                _logger.warning(
                    "settings.json: unsupported %s %r; using %r",
                    spec.dotted, value, spec.default,
                )
                value = spec.default
            kwargs[spec.field] = value
    return AutoSwitchSettings(**kwargs)


def _read_raw(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _logger.warning("Could not read %s (%s); using defaults", path, e)
        return {}
    if not isinstance(raw, dict):
        _logger.warning("%s is not a JSON object; using defaults", path)
        return {}
    return raw


def load_settings(backup_root: Path) -> AutoSwitchSettings:
    """Load the autoswitch section; missing/corrupt file or fields → defaults."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        return AutoSwitchSettings()
    kwargs = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        if json_key in section:
            kwargs[field] = section[json_key]
    try:
        settings = AutoSwitchSettings(**kwargs)
    except TypeError:
        settings = AutoSwitchSettings()
    return _clamped(settings)


def load_ui_settings(backup_root: Path) -> UiSettings:
    """Load the ui section; missing/corrupt file or unknown theme → default."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("ui")
    default = UiSettings()
    if not isinstance(section, dict):
        return default
    theme = section.get("theme", default.theme)
    if theme not in SETTING_SPECS["ui.theme"].choices:
        _logger.warning(
            "settings.json: unsupported ui.theme %r; using %r",
            theme, default.theme,
        )
        return default
    return UiSettings(theme=theme)


def load_storage_settings(backup_root: Path) -> StorageSettings:
    """Load the storage section; missing/corrupt file or unknown backend → default."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("storage")
    default = StorageSettings()
    if not isinstance(section, dict):
        return default
    backend = section.get("backend", default.backend)
    if backend not in SETTING_SPECS["storage.backend"].choices:
        _logger.warning(
            "settings.json: unsupported storage.backend %r; using %r",
            backend, default.backend,
        )
        return default
    return StorageSettings(backend=backend)


def save_settings(backup_root: Path, settings: AutoSwitchSettings) -> None:
    """Write the autoswitch section, preserving unknown keys and sections."""
    path = settings_path(backup_root)
    raw = _read_raw(path)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        section = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        section[json_key] = getattr(settings, field)
    raw["autoswitch"] = section
    atomic_write_json(path, raw)


def setting_spec(dotted_key: str) -> SettingSpec:
    """Look up a spec by dotted key; unknown keys raise with the valid list."""
    spec = SETTING_SPECS.get(dotted_key)
    if spec is None:
        raise ConfigError(
            f"unknown setting '{dotted_key}'\n"
            f"Valid keys: {', '.join(SETTING_SPECS)}"
        )
    return spec


_BOOL_WORDS = {
    "true": True, "1": True, "yes": True,
    "false": False, "0": False, "no": False,
}


def parse_setting_value(spec: SettingSpec, raw_value: str):
    """Strictly parse a CLI-provided string for `cswap config set`.

    Unlike the forgiving clamp on load, out-of-range or mistyped values raise
    ConfigError so the user learns about the problem when setting the value,
    not by silently degraded behavior at `cswap auto` time.
    """
    if spec.kind == "bool":
        # Never bool(str): bool("false") is True.
        parsed = _BOOL_WORDS.get(raw_value.strip().lower())
        if parsed is None:
            raise ConfigError(
                f"{spec.dotted} expects true or false (or 1/0, yes/no), "
                f"got '{raw_value}'"
            )
        return parsed
    if spec.kind == "choice":
        if raw_value not in spec.choices:
            raise ConfigError(
                f"{spec.dotted} must be one of: {', '.join(spec.choices)}"
            )
        return raw_value
    if spec.kind == "string":
        value = raw_value.strip()
        if not value:
            raise ConfigError(
                f"{spec.dotted} expects a non-empty value; use "
                f"'cswap config unset {spec.dotted}' to clear it"
            )
        return value
    try:
        value = int(raw_value) if spec.kind == "int" else float(raw_value)
    except ValueError:
        noun = "an integer" if spec.kind == "int" else "a number"
        raise ConfigError(
            f"{spec.dotted} expects {noun}, got '{raw_value}'"
        ) from None
    if not spec.lo <= value <= spec.hi:
        raise ConfigError(
            f"{spec.dotted} must be between {format_setting_value(spec.lo)} "
            f"and {format_setting_value(spec.hi)}"
        )
    return value


def format_setting_value(value) -> str:
    """Render a settings value the way settings.json writes it."""
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_raw_for_write(path: Path) -> dict:
    """Raw read for the config write path: a corrupt file errors, never {}.

    ``_read_raw``'s degrade-to-defaults is right for reads, but a
    read-modify-write starting from ``{}`` would replace a malformed (and
    maybe hand-recoverable) file with a near-empty one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"could not read {path}: {e}") from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{path} is not valid JSON ({e}); fix or delete it before "
            "changing settings"
        ) from e
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} is not a JSON object; fix or delete it before "
            "changing settings"
        )
    return raw


def set_setting(backup_root: Path, dotted_key: str, raw_value: str):
    """Validate and persist one key for `cswap config set`; returns the value.

    Writes only the given key (plus schemaVersion) — deliberately not
    ``save_settings``, which writes every known key and would freeze the
    current defaults into the file, pinning users to them if a later version
    changes a default. Unknown keys and sections in the file survive.
    """
    spec = setting_spec(dotted_key)
    value = parse_setting_value(spec, raw_value)
    path = settings_path(backup_root)
    raw = _read_raw_for_write(path)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = raw.get(spec.section)
    if not isinstance(section, dict):
        section = {}
    section[spec.json_key] = value
    raw[spec.section] = section
    atomic_write_json(path, raw)
    return value


def unset_setting(backup_root: Path, dotted_key: str) -> bool:
    """Remove one key from settings.json; False if it wasn't set (no write)."""
    spec = setting_spec(dotted_key)
    path = settings_path(backup_root)
    raw = _read_raw_for_write(path)
    section = raw.get(spec.section)
    if not isinstance(section, dict) or spec.json_key not in section:
        return False
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    del section[spec.json_key]
    if not section:
        del raw[spec.section]
    atomic_write_json(path, raw)
    return True


def effective_settings(backup_root: Path) -> list[tuple[SettingSpec, object, bool]]:
    """(spec, effective value, explicitly set?) per key, in registry order.

    "Set" means the key is present in the raw file — an explicit value equal
    to the default still counts — so `cswap config`'s "(default)" marker
    reflects the file, not value equality.
    """
    raw = _read_raw(settings_path(backup_root))
    loaded = {
        "autoswitch": load_settings(backup_root),
        "ui": load_ui_settings(backup_root),
        "storage": load_storage_settings(backup_root),
    }
    rows = []
    for spec in SETTING_SPECS.values():
        section = raw.get(spec.section)
        is_set = isinstance(section, dict) and spec.json_key in section
        rows.append((spec, getattr(loaded[spec.section], spec.field), is_set))
    return rows


def merged_with_cli(settings: AutoSwitchSettings, args) -> AutoSwitchSettings:
    """Overlay non-None CLI overrides (argparse Namespace) onto settings."""
    overrides = {}
    for attr, field in (
        ("threshold", "threshold"),
        ("interval", "interval_seconds"),
        ("cooldown", "cooldown_seconds"),
        ("include_api_key_accounts", "include_api_key_accounts"),
        ("model", "model"),
        ("strategy", "strategy"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            overrides[field] = value
    if not overrides:
        return settings
    return _clamped(dataclasses.replace(settings, **overrides))


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON with the backup dir's 0600/0700 modes.

    Shared by settings.json and the autoswitch state file (and any future
    machine-local state files beside them).

    **Writes THROUGH a symlink, never over it.** A rename swaps a directory
    ENTRY and does not follow links, so renaming onto a symlinked path
    DETACHES the link: the write succeeds, the content is right, and the
    link target silently stops receiving updates — until something restores
    the link (a dotfiles deploy), taking every change written since with
    it. Same shape as #192/#193, which fixed ``session.py``'s own writer;
    this is the shared JSON writer. Three consequences, each deliberate:

    - A DANGLING link still writes where it points; linking a path is a
      request to write there.
    - The temp file is created beside the RESOLVED target, so the rename
      stays on one filesystem and remains atomic (beside the LINK it would
      hit EXDEV whenever the target lives on another mount).
    - The 0700 hardening stays on the directory cswap owns. Applying it to
      the resolved parent would narrow a directory belonging to something
      else, and raise ``PermissionError`` outright when that parent is not
      ours to chmod. The written file still gets 0600, and ``mkstemp``
      creates it 0600 to begin with, so the secret is never exposed.
    """
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        # `path.parent`, NOT the target's: see the docstring.
        os.chmod(path.parent, 0o700)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        os.close(fd)
        fd = -1
        replace_with_retry(tmp_path, str(target))
        if sys.platform != "win32":
            os.chmod(str(target), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
