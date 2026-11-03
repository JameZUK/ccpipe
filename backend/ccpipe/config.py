"""Persistent ccpipe app configuration.

Settings the user changes via the settings modal land here (TTS voice,
speech rate, default TTS enabled). Per-device display preferences (font
size, cursor style) live in browser localStorage instead — they're not
worth round-tripping through the server.

Storage: ``~/.local/state/ccpipe/config.json`` (or
``$CCPIPE_CONFIG_FILE``). 0600 permissions, atomic-replace on save.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .paths import state_dir

log = logging.getLogger(__name__)

CONFIG_FILE_ENV = "CCPIPE_CONFIG_FILE"


# Allowed values for TtsConfig.scope. Controls how much of the
# assistant's response goes to TTS. See ccpipe.tts._apply_scope.
VALID_SCOPES = ("full", "last_paragraph", "last_sentence", "last_question", "off")


@dataclass
class TtsConfig:
    voice: str = "bf_emma"
    speech_rate: float = 1.0  # Kokoro 'speed' param; clamped 0.5..2.0
    enabled: bool = True       # default mute state; UI can override per-session
    scope: str = "last_paragraph"  # see VALID_SCOPES


@dataclass
class FsConfig:
    """Settings for the file-transfer side-panel."""
    # Maximum size (megabytes) we'll accept for a single upload. Edit
    # this in ~/.local/state/ccpipe/config.json to change without a
    # restart. Hard floor of 1 MB so the inline editor still works.
    upload_limit_mb: int = 500


@dataclass
class MicConfig:
    """Voice-input behaviour, tunable from Settings → Voice input.

    The user-visible knobs surfacing in the UI:
      - ``auto_stop_enabled``: whether the client's silence detector
        fires `mic_stop` automatically on sustained silence. When
        false, the mic is strictly tap-to-stop.
      - ``silence_ms``: how long sustained silence must run before the
        client-side VAD trips (only relevant when auto-stop is on).
      - ``drain_pad_ms``: extra wall-time the BACKEND waits after the
        estimated audio-drain completes before writing the release-PTT
        keystroke to the PTY. Buys time for Pulse's internal buffer
        + claude's STT finalisation to settle. The key knob if you
        find tail words still being cut on submission.
      - ``max_recording_seconds``: client-side hard cap; the mic
        force-stops after this many seconds even with continuous voice.
        Safety net for forgotten mics.
    """
    auto_stop_enabled: bool = True
    silence_ms: int = 2500
    drain_pad_ms: int = 1500
    max_recording_seconds: int = 60


BOOT_MAX = 32                       # = tmux.MAX_SESSIONS
IDLE_MAX_MIN = 30 * 24 * 60         # 30 days


def _idle_minutes(v: Any, default: int) -> int:
    """0 (never) or 15 min .. 30 days."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return 0 if n <= 0 else max(15, min(IDLE_MAX_MIN, n))


@dataclass
class SessionsConfig:
    """Session lifecycle, tunable from Settings → Sessions (lifecycle.py).

      - ``boot_sticky`` / ``boot_other``: when the machine boots (tmux has
        no sessions), start only this many of the most recently used sticky
        / other sessions. The rest are listed as stopped and resume —
        same conversation — when opened.
      - ``idle_stop_other_min`` / ``idle_stop_sticky_min``: stop a session
        once it has been completely idle this long (0 = never). Idle means
        no terminal attached, no output or input, Claude not working, no
        command / task running and no CPU use — see lifecycle.busy_reason.
        Sticky sessions get the longer default.
    """
    boot_sticky: int = 5
    boot_other: int = 0
    idle_stop_other_min: int = 120
    idle_stop_sticky_min: int = 1440

    @classmethod
    def from_dict(cls, d: Any) -> "SessionsConfig":
        c = cls()
        if not isinstance(d, dict):
            return c
        for k in ("boot_sticky", "boot_other"):
            try:
                setattr(c, k, max(0, min(BOOT_MAX, int(d.get(k, getattr(c, k))))))
            except (TypeError, ValueError):
                pass
        c.idle_stop_other_min = _idle_minutes(d.get("idle_stop_other_min", c.idle_stop_other_min),
                                              c.idle_stop_other_min)
        c.idle_stop_sticky_min = _idle_minutes(d.get("idle_stop_sticky_min", c.idle_stop_sticky_min),
                                               c.idle_stop_sticky_min)
        return c


@dataclass
class AppConfig:
    tts: TtsConfig = field(default_factory=TtsConfig)
    fs: FsConfig = field(default_factory=FsConfig)
    mic: MicConfig = field(default_factory=MicConfig)
    sessions: SessionsConfig = field(default_factory=SessionsConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tts": asdict(self.tts),
            "fs": asdict(self.fs),
            "mic": asdict(self.mic),
            "sessions": asdict(self.sessions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        tts_data = data.get("tts", {}) if isinstance(data, dict) else {}
        if not isinstance(tts_data, dict):
            tts_data = {}
        tts = TtsConfig()
        if isinstance(tts_data.get("voice"), str) and tts_data["voice"]:
            tts.voice = tts_data["voice"]
        try:
            rate = float(tts_data.get("speech_rate", 1.0))
            tts.speech_rate = max(0.5, min(2.0, rate))
        except (TypeError, ValueError):
            pass
        if isinstance(tts_data.get("enabled"), bool):
            tts.enabled = tts_data["enabled"]
        scope = tts_data.get("scope")
        if isinstance(scope, str) and scope in VALID_SCOPES:
            tts.scope = scope
        fs_data = data.get("fs", {}) if isinstance(data, dict) else {}
        if not isinstance(fs_data, dict):
            fs_data = {}
        fs = FsConfig()
        try:
            limit = int(fs_data.get("upload_limit_mb", fs.upload_limit_mb))
            fs.upload_limit_mb = max(1, min(10_240, limit))   # 1MB .. 10GB
        except (TypeError, ValueError):
            pass
        mic_data = data.get("mic", {}) if isinstance(data, dict) else {}
        if not isinstance(mic_data, dict):
            mic_data = {}
        mic = MicConfig()
        if isinstance(mic_data.get("auto_stop_enabled"), bool):
            mic.auto_stop_enabled = mic_data["auto_stop_enabled"]
        try:
            mic.silence_ms = max(200, min(15000, int(
                mic_data.get("silence_ms", mic.silence_ms))))
        except (TypeError, ValueError):
            pass
        try:
            mic.drain_pad_ms = max(0, min(10000, int(
                mic_data.get("drain_pad_ms", mic.drain_pad_ms))))
        except (TypeError, ValueError):
            pass
        try:
            mic.max_recording_seconds = max(5, min(600, int(
                mic_data.get("max_recording_seconds",
                             mic.max_recording_seconds))))
        except (TypeError, ValueError):
            pass
        sessions = SessionsConfig.from_dict(data.get("sessions") if isinstance(data, dict) else None)
        return cls(tts=tts, fs=fs, mic=mic, sessions=sessions)


_state_dir = state_dir


def _default_config_path() -> Path:
    return _state_dir() / "config.json"


def config_path() -> Path:
    # L13: strip + treat empty string as unset (Path("") would silently
    # resolve to cwd).
    override = (os.environ.get(CONFIG_FILE_ENV) or "").strip() or None
    return Path(override) if override else _default_config_path()


_cache: AppConfig | None = None


def load() -> AppConfig:
    """Read the persisted config, falling back to env defaults on first
    run. Memoized; call ``reset_cache()`` after a manual edit."""
    global _cache
    if _cache is not None:
        return _cache
    path = config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            _cache = AppConfig.from_dict(data if isinstance(data, dict) else {})
        except (OSError, ValueError) as exc:
            log.warning("ignoring malformed config at %s: %s", path, exc)
            _cache = AppConfig()
    else:
        # First-run defaults seeded from the historical env var so existing
        # systemd drop-ins still take effect until the user picks a voice
        # in the UI.
        voice = (os.environ.get("CCPIPE_TTS_VOICE", "") or "bf_emma").strip()
        _cache = AppConfig(tts=TtsConfig(voice=voice or "bf_emma"))
    return _cache


def load_for_edit() -> AppConfig:
    """A private copy of the config to change and pass to ``save()``.
    Editing ``load()``'s object directly changes the live settings even
    when the save then fails — the file and the running app disagree until
    a restart."""
    return copy.deepcopy(load())


def save(config: AppConfig) -> None:
    """Atomic-replace persist. Updates the in-memory cache — only once the
    file is written."""
    global _cache
    path = config_path()
    # Mirror auth._ensure_state_dir: tighten the state directory to 0700
    # so a local non-root attacker can't list it / read mtimes.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(config.to_dict(), indent=2).encode() + b"\n")
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _cache = config


def reset_cache() -> None:
    """For tests: clear the memoized config so the next load() re-reads."""
    global _cache
    _cache = None
