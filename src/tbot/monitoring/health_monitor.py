"""Host-side Telegram HEALTH-ALERT monitor (send-only; no trade alerts, no inbound commands).

Runs as ``tbot-health-monitor.service`` on the server, outside every recorder container, and
watches four classes of problem, then tells Parham about them on Telegram:

1. a recorder stream is stale or erroring (Tabdeal recorder, LBank recorder),
2. a backup failed, failed its integrity check, is stale, or its systemd unit is ``failed``,
3. a filesystem is above the disk threshold (default 80 %),
4. a recorder container is exited / restarting / missing / unhealthy, or docker itself is down.

Design notes
------------
* **Stdlib only** (urllib, subprocess, json): the unit runs under the system ``python3`` with no
  virtualenv, so there is nothing to break when the project's venv is rebuilt. CLAUDE.md section 7
  (no new dependencies) is respected.
* **Pure checks, injectable edges.** Every check takes a ``Settings`` and a ``Probes`` bundle
  (clock, command runner, file reader, directory lister, disk usage) and returns ``Problem``
  objects; the Telegram sender is a plain callable. Tests drive all of it with fakes.
* **Stream staleness is read from ``docker inspect``.** The recorders already evaluate their own
  heartbeat inside the container (``deploy/healthcheck.py``, ``scripts/lbank_healthcheck.py``,
  every 30 s) and publish the verdict plus the failing reason as the container's Health status /
  last Health log line, so the monitor reuses that instead of re-implementing the thresholds. As an
  independent second signal it also reads each heartbeat file read-only through the docker volume's
  host mountpoint and flags a heartbeat older than ``heartbeat_max_age_s``.
* **Secrets.** The bot token is part of the Telegram URL path. The URL is never logged, never put
  in an exception message and never chained as ``__cause__``; failures are reduced to a short
  reason plus the HTTP status. Log output additionally passes a redaction filter.
* **Never crash the loop.** Each check is wrapped (a crash becomes a ``monitor:<check>`` problem), a
  Telegram failure only logs and backs off, and a missing ``docker`` / ``systemctl`` binary is itself
  reported as an alert.
* **Unknown is not clear.** When a check fails as a whole (docker down, check crashed) the alerts it
  owns stay open: it names the key prefixes to freeze (``Problem.freeze``) and the dedupe logic neither
  counts those keys towards RESOLVED nor resets them. One recorder is one key (``recorder:<name>``), so
  its container state and its stream state can never clear each other.
* **Hysteresis.** Container and disk alerts need ``confirm_cycles`` consecutive bad cycles; stream and
  backup failures alert at once. A disk alert clears only ``disk_clear_margin`` points below the limit.
* **No secrets in children.** ``docker`` / ``systemctl`` run with a scrubbed environment; heartbeat and
  status files are read with ``O_NOFOLLOW`` and a size cap.
* **Retiring a recorder.** ``TBOT_MONITOR_RETIRED=tabdeal`` (comma-separated target names; set it in a
  systemd drop-in, not in the secrets file) removes that recorder from every check, including the
  ``backup:trades`` check that belongs to Tabdeal. Open alerts for the retired keys are closed once with a
  "RETIRED" message (never "RESOLVED"); the start message and the start log line list what is retired.
* **Optional dead-man ping.** If ``TBOT_DEADMAN_URL`` is set, one GET is made after every completed
  cycle (healthchecks.io style); failures are ignored and the URL is never logged.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import http.client
import json
import logging
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

__all__ = [
    "CmdResult",
    "Collected",
    "ConfigError",
    "DeadmanPinger",
    "DiskUsage",
    "Memory",
    "Monitor",
    "Probes",
    "Problem",
    "Settings",
    "StateStore",
    "Target",
    "TelegramError",
    "TelegramSender",
    "check_disks",
    "check_lbank_backup",
    "check_recorders",
    "check_trades_backup",
    "check_units",
    "collect",
    "collect_problems",
    "evaluate_container",
    "evaluate_lbank_status",
    "is_valid_token",
    "load_credentials",
    "load_settings",
    "main",
    "redact",
    "register_secret",
    "telegram_get",
]

log = logging.getLogger("tbot.health_monitor")

# ---------------------------------------------------------------------------------------------
# Defaults / settings
# ---------------------------------------------------------------------------------------------

ENV_TOKEN = "TBOT_TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TBOT_TELEGRAM_CHAT_ID"
ENV_RETIRED = "TBOT_MONITOR_RETIRED"  # non-secret: set in a systemd drop-in, never in the secrets file

TELEGRAM_BASE_URL = "https://api.telegram.org"
_STARTING_GRACE_S = 600.0  # a container may report Health "starting" for this long after start
_START_NOTICE_MIN_GAP_S = 300.0  # no "monitor started" message more often than this (crash loops)
_MAX_EVENTS_PER_CYCLE = 8
_MAX_BACKOFF_S = 900.0
_MAX_TEXT_CHARS = 900

LBANK_STATUS_FILE = Path("/var/backups/tbot/lbank/status.json")
TRADES_BACKUP_DIR = Path("/var/backups/tbot/trades")  # deploy/systemd/tbot-trades-backup.service
_TRADES_BACKUP_RE = re.compile(r"^trades-(\d{8}T\d{6}Z)\.sqlite$")  # deploy/backup_trades.py
DEFAULT_STATE_DIR = Path("/var/lib/tbot-health-monitor")
DEFAULT_DISK_PATHS = (Path("/"), Path("/var/lib/docker"), Path("/var/backups/tbot"))
BACKUP_UNITS = ("tbot-trades-backup.service", "tbot-lbank-backup.service")


@dataclass(frozen=True)
class Target:
    """One recorder: its container, the docker volume holding its heartbeat, and how to read it."""

    name: str
    container: str
    volume: str
    heartbeat_rel: str
    ts_field: str
    ts_kind: str  # "iso" (ISO-8601 string) or "ms" (epoch milliseconds)


DEFAULT_TARGETS = (
    Target("tabdeal", "tbot-recorder", "tbot-data", "tabdeal/heartbeat.json", "last_poll_ts", "iso"),
    Target("lbank", "tbot-lbank-recorder", "tbot-lbank-data", "lbank/heartbeat.json", "written_ms", "ms"),
)


@dataclass(frozen=True)
class Settings:
    interval_s: float = 60.0
    disk_max_pct: float = 80.0
    repeat_hours: float = 6.0
    backup_max_age_hours: float = 36.0
    heartbeat_max_age_s: float = 300.0
    resolve_cycles: int = 2
    confirm_cycles: int = 2  # consecutive bad cycles before a container / disk alert is raised
    disk_clear_margin: float = 2.0  # a disk alert clears only this many points below the limit
    targets: tuple[Target, ...] = DEFAULT_TARGETS
    disk_paths: tuple[Path, ...] = DEFAULT_DISK_PATHS
    lbank_status_file: Path = LBANK_STATUS_FILE
    trades_backup_dir: Path = TRADES_BACKUP_DIR
    state_dir: Path = DEFAULT_STATE_DIR
    hostname: str = "server"
    deadman_url: str = field(default="", repr=False)  # secret-ish: never logged or printed
    retired: tuple[str, ...] = ()  # target names no longer monitored (TBOT_MONITOR_RETIRED)

    @property
    def active_targets(self) -> tuple[Target, ...]:
        """The recorder targets still being monitored (everything not retired)."""
        return tuple(t for t in self.targets if t.name not in self.retired)

    @property
    def retired_keys(self) -> frozenset[str]:
        """Alert keys that belong to retired targets; they are never checked, planned or resolved."""
        keys: set[str] = set()
        for target in self.targets:
            if target.name in self.retired:
                keys.add(recorder_key(target))
                keys.update(_TARGET_BACKUP_KEYS.get(target.name, ()))
        return frozenset(keys)


# Alert keys of the backup check that belongs to a target (retiring the target retires them too).
_TARGET_BACKUP_KEYS: dict[str, tuple[str, ...]] = {"tabdeal": ("backup:trades",)}


class ConfigError(Exception):
    """Invalid configuration. The message never contains a secret value."""


def _load_retired(env: Mapping[str, str]) -> tuple[str, ...]:
    """``TBOT_MONITOR_RETIRED``: comma-separated target names. An unknown name is a ``ConfigError``
    (a typo must never silently leave a recorder monitored, or hide the wrong one)."""
    raw = env.get(ENV_RETIRED, "")
    names = tuple(dict.fromkeys(n.strip().lower() for n in raw.split(",") if n.strip()))
    known = [t.name for t in DEFAULT_TARGETS]
    unknown = [n for n in names if n not in known]
    if unknown:
        shown = ", ".join(short(n, 40) for n in unknown[:5])
        raise ConfigError(f"{ENV_RETIRED} has unknown target name(s): {shown} (valid: {', '.join(known)})")
    return names


def _env_float(env: Mapping[str, str], name: str, default: float, minimum: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("ignoring non-numeric %s, using default %s", name, default)
        return default
    if value < minimum:
        log.warning("ignoring %s below %s, using default %s", name, minimum, default)
        return default
    return value


ENV_DEADMAN_URL = "TBOT_DEADMAN_URL"


def _load_deadman_url(env: Mapping[str, str]) -> str:
    """The optional dead-man URL (http/https only). Never logged; invalid values are dropped."""
    raw = env.get(ENV_DEADMAN_URL, "").strip()
    if not raw:
        return ""
    try:
        parts = urllib.parse.urlsplit(raw)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        valid = False
    if not valid:
        log.warning("ignoring %s: not an http(s) URL", ENV_DEADMAN_URL)
        return ""
    register_secret(raw)
    return raw


def load_settings(env: Mapping[str, str], *, hostname: str | None = None) -> Settings:
    """Optional tuning from the environment; every value has a safe default.

    Raises ``ConfigError`` for an unknown name in ``TBOT_MONITOR_RETIRED``."""
    paths_raw = env.get("TBOT_MONITOR_DISK_PATHS", "").strip()
    disk_paths = tuple(Path(p.strip()) for p in paths_raw.split(",") if p.strip()) or DEFAULT_DISK_PATHS
    return Settings(
        confirm_cycles=int(_env_float(env, "TBOT_MONITOR_CONFIRM_CYCLES", 2.0, 1.0)),
        disk_clear_margin=_env_float(env, "TBOT_MONITOR_DISK_CLEAR_MARGIN", 2.0, 0.0),
        deadman_url=_load_deadman_url(env),
        interval_s=_env_float(env, "TBOT_MONITOR_INTERVAL_S", 60.0, 5.0),
        disk_max_pct=_env_float(env, "TBOT_MONITOR_DISK_MAX_PCT", 80.0, 1.0),
        repeat_hours=_env_float(env, "TBOT_MONITOR_REPEAT_HOURS", 6.0, 0.01),
        backup_max_age_hours=_env_float(env, "TBOT_MONITOR_BACKUP_MAX_AGE_HOURS", 36.0, 1.0),
        heartbeat_max_age_s=_env_float(env, "TBOT_MONITOR_HEARTBEAT_MAX_AGE_S", 300.0, 30.0),
        resolve_cycles=int(_env_float(env, "TBOT_MONITOR_RESOLVE_CYCLES", 2.0, 1.0)),
        disk_paths=disk_paths,
        lbank_status_file=Path(env.get("TBOT_MONITOR_LBANK_STATUS_FILE") or LBANK_STATUS_FILE),
        trades_backup_dir=Path(env.get("TBOT_MONITOR_TRADES_BACKUP_DIR") or TRADES_BACKUP_DIR),
        state_dir=Path(env.get("TBOT_MONITOR_STATE_DIR") or DEFAULT_STATE_DIR),
        hostname=hostname or socket.gethostname(),
        retired=_load_retired(env),
    )


_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
_CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")


def is_valid_token(token: str) -> bool:
    """True if ``token`` has the shape of a Telegram bot token (``<digits>:<letters/digits>``)."""
    return _TOKEN_RE.match(token) is not None


def load_credentials(env: Mapping[str, str]) -> tuple[str, str]:
    """Return ``(token, chat_id)`` or raise ``ConfigError`` (never echoing either value)."""
    token = env.get(ENV_TOKEN, "").strip()
    chat_id = env.get(ENV_CHAT_ID, "").strip()
    if not token or not chat_id:
        raise ConfigError(
            f"{ENV_TOKEN} and {ENV_CHAT_ID} must both be set (see deploy/health_monitor.env.example)"
        )
    if not is_valid_token(token):
        raise ConfigError(f"{ENV_TOKEN} does not look like a Telegram bot token (<digits>:<letters/digits>)")
    if not _CHAT_ID_RE.match(chat_id):
        raise ConfigError(f"{ENV_CHAT_ID} must be a numeric chat id (e.g. 123456789 or -1001234567890)")
    register_secret(token)
    return token, chat_id


# ---------------------------------------------------------------------------------------------
# Log redaction and JSON logging (stdlib only)
# ---------------------------------------------------------------------------------------------

_SECRETS: tuple[str, ...] = ()
_SECRETS_LOCK = threading.Lock()
_TOKEN_IN_TEXT_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}")
_BOT_URL_RE = re.compile(r"/bot[^/\s'\"]+")


def register_secret(value: str) -> None:
    """Remember a raw secret so ``redact`` removes it from any text, wherever it appears."""
    global _SECRETS
    if len(value) < 8:
        return
    with _SECRETS_LOCK:
        if value not in _SECRETS:
            _SECRETS = (*_SECRETS, value)


def redact(text: str) -> str:
    """Remove registered secrets, bot-token shapes and ``/bot<token>`` URL segments from ``text``."""
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    text = _BOT_URL_RE.sub("/bot***", text)
    return _TOKEN_IN_TEXT_RE.sub("***", text)


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Tracebacks are NOT rendered (they can embed request URLs);
    only the exception type name is kept."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "urllib3", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------------------------
# Probes (injectable edges)
# ---------------------------------------------------------------------------------------------


class ToolError(Exception):
    """An external command could not be run (missing binary, permission, timeout)."""

    def __init__(self, tool: str, reason: str) -> None:
        super().__init__(f"{tool}: {reason}")
        self.tool = tool
        self.reason = reason


@dataclass(frozen=True)
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DiskUsage:
    used: int
    free: int
    device: int

    @property
    def pct_used(self) -> float:
        total = self.used + self.free
        return 100.0 * self.used / total if total else 100.0


CommandRunner = Callable[[Sequence[str]], CmdResult]

# The only variables a child (docker / systemctl) may inherit. Everything else -- in particular
# TBOT_TELEGRAM_BOT_TOKEN / TBOT_TELEGRAM_CHAT_ID / TBOT_DEADMAN_URL -- stays in this process.
_CHILD_ENV_ALLOWED = ("PATH", "HOME", "LANG", "LC_ALL", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    env = {name: source[name] for name in _CHILD_ENV_ALLOWED if name in source}
    env.setdefault("PATH", _DEFAULT_PATH)
    return env


def run_command(args: Sequence[str]) -> CmdResult:
    """Run an argument-list command (never a shell) with a hard timeout and a scrubbed environment."""
    try:
        done = subprocess.run(
            list(args), capture_output=True, text=True, timeout=20.0, check=False, env=_child_env()
        )
    except FileNotFoundError as exc:
        raise ToolError(args[0], "not installed / not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(args[0], "timed out") from exc
    except OSError as exc:
        raise ToolError(args[0], type(exc).__name__) from exc
    return CmdResult(done.returncode, done.stdout, done.stderr)


MAX_READ_BYTES = 1024 * 1024  # heartbeat and status files are tiny; anything bigger is not ours


def _read_text(path: Path, max_bytes: int = MAX_READ_BYTES) -> str:
    """Read a small regular file. A symlink as the final path component is refused (O_NOFOLLOW; the
    parent directories are not checked), as is anything that is not a regular file or is too large."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OSError(errno.EFBIG, "file too large")
    return data.decode("utf-8", errors="replace")


def _list_names(path: Path) -> list[str]:
    return [entry.name for entry in path.iterdir()]


def _disk_usage(path: Path) -> DiskUsage | None:
    try:
        st = os.stat(path)  # noqa: PTH116 - only the device id is needed
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return DiskUsage(used=usage.used, free=usage.free, device=st.st_dev)


@dataclass
class Memory:
    """What checks may remember between cycles. The ``Monitor`` loads and persists the dicts.

    * ``restart_counts``: last *communicated* docker ``RestartCount`` per container (crash-loop
      detection). This only advances once the Monitor has actually sent a message mentioning it
      (``Monitor._commit_restart_count``) -- never merely because a cycle observed it -- so a
      Telegram back-off or the per-cycle message cap cannot silently swallow a restart tick.
    * ``pending_restart_counts``: the RestartCount observed THIS cycle, not yet committed into
      ``restart_counts``. Purely a hand-off from the check function to the ``Monitor``; it is
      recomputed from live docker data every cycle and is never persisted across a restart.
    * ``anchors``: ISO timestamps of reference points (e.g. when the backup status file was last seen),
    * ``open_keys``: keys with an open alert right now (set by the ``Monitor`` before every cycle; the
      disk check uses it to apply the clear margin).
    """

    restart_counts: dict[str, int] = field(default_factory=dict)
    pending_restart_counts: dict[str, int] = field(default_factory=dict)
    anchors: dict[str, str] = field(default_factory=dict)
    open_keys: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Probes:
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    run: CommandRunner = run_command
    read_text: Callable[[Path], str] = _read_text
    list_names: Callable[[Path], list[str]] = _list_names
    disk: Callable[[Path], DiskUsage | None] = _disk_usage
    memory: Memory = field(default_factory=Memory)


@dataclass(frozen=True)
class Problem:
    """A current problem. ``key`` is the dedupe identity; ``summary`` is the short human text.

    * ``confirm``: needs ``confirm_cycles`` consecutive bad cycles before it alerts (flapping guard),
    * ``freeze``: key prefixes this check could NOT evaluate (it failed as a whole); tracked alerts
      under them are neither cleared nor counted towards RESOLVED this cycle,
    * ``silent``: not an alert -- it only carries ``freeze``.
    """

    key: str
    summary: str
    confirm: bool = False
    freeze: tuple[str, ...] = ()
    silent: bool = False


@dataclass(frozen=True)
class Collected:
    """Result of one pass over all checks: current problems plus the key prefixes left unknown."""

    problems: dict[str, Problem]
    frozen: tuple[str, ...] = ()

    def is_frozen(self, key: str) -> bool:
        return any(key.startswith(prefix) for prefix in self.frozen)


def short(text: str, limit: int = 200) -> str:
    """One printable line, truncated; used for anything that originates outside this process."""
    cleaned = " ".join(text.split())
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (docker nanoseconds and a trailing Z included) as UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(_FRACTION_RE.sub(r"\1", value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# Checks: disks
# ---------------------------------------------------------------------------------------------


def check_disks(settings: Settings, probes: Probes) -> list[Problem]:
    """Class 3: filesystem usage above the threshold (one report per distinct filesystem).

    An open alert keeps going until usage is ``disk_clear_margin`` points BELOW the limit, so a disk
    hovering at the limit does not flap. A path that cannot be measured is "unknown": it raises no
    alert, but a tracked alert for it is frozen instead of being counted as cleared.
    """
    problems: list[Problem] = []
    seen_devices: set[int] = set()
    for path in settings.disk_paths:
        key = f"disk:{path}"
        usage = probes.disk(path)
        if usage is None:
            problems.append(Problem(key, f"cannot measure {path}", freeze=(key,), silent=True))
            continue
        if usage.device in seen_devices:
            continue
        seen_devices.add(usage.device)
        limit = settings.disk_max_pct
        is_open = key in probes.memory.open_keys
        if is_open:
            limit -= settings.disk_clear_margin
        if usage.pct_used > limit:
            summary = f"disk {path} is {usage.pct_used:.1f}% used (limit {settings.disk_max_pct:g}%"
            summary += f", clears below {limit:g}%)" if is_open else ")"
            problems.append(Problem(key, summary, confirm=True))
    return problems


# ---------------------------------------------------------------------------------------------
# Checks: recorder containers and streams
# ---------------------------------------------------------------------------------------------


def recorder_key(target: Target) -> str:
    return f"recorder:{target.name}"


def _last_health_output(health: Mapping[str, Any]) -> str:
    entries = health.get("Log")
    if isinstance(entries, list) and entries and isinstance(entries[-1], dict):
        return short(str(entries[-1].get("Output", "")))
    return ""


def evaluate_container(target: Target, state: object, now: datetime) -> list[Problem]:
    """Classes 1 and 4 from one ``docker inspect --format '{{json .State}}'`` document."""
    container = target.container
    key = recorder_key(target)  # ONE key per recorder: container state and stream state share it
    if not isinstance(state, dict):
        return [Problem(key, f"{container}: unreadable docker state", confirm=True)]
    status = state.get("Status")
    exit_code = state.get("ExitCode")
    if state.get("Restarting") is True or status == "restarting":
        return [Problem(key, f"{container} is restarting (last exit code {exit_code})", confirm=True)]
    if status != "running":
        return [Problem(key, f"{container} is {status} (exit code {exit_code})", confirm=True)]
    health = state.get("Health")
    if not isinstance(health, dict):
        return [Problem(key, f"{container} has no healthcheck status")]
    health_status = health.get("Status")
    if health_status == "unhealthy":
        return [Problem(key, f"{container} unhealthy: {_last_health_output(health)}")]
    if health_status == "starting":
        started = parse_iso(state.get("StartedAt"))
        if started is not None and (now - started).total_seconds() > _STARTING_GRACE_S:
            minutes = _STARTING_GRACE_S / 60
            return [Problem(key, f"{container} still 'starting' after {minutes:.0f} min")]
        return []
    if health_status != "healthy":
        return [Problem(key, f"{container} has unknown health status {short(str(health_status), 40)}")]
    return []


def _heartbeat_age_s(target: Target, payload: object, now: datetime) -> float | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get(target.ts_field)
    if target.ts_kind == "ms":
        if not isinstance(raw, int) or isinstance(raw, bool):
            return None
        return now.timestamp() - raw / 1000.0
    parsed = parse_iso(raw)
    return None if parsed is None else (now - parsed).total_seconds()


def check_heartbeat(target: Target, settings: Settings, probes: Probes) -> list[Problem]:
    """Independent read-only look at the heartbeat file through the volume's host mountpoint."""
    key = recorder_key(target)
    res = probes.run(["docker", "volume", "inspect", target.volume, "--format", "{{.Mountpoint}}"])
    mountpoint = res.stdout.strip()
    if res.returncode != 0 or not mountpoint:
        return [Problem(key, f"cannot locate docker volume {target.volume}")]
    path = Path(mountpoint) / target.heartbeat_rel
    try:
        payload = json.loads(probes.read_text(path))
    except (OSError, ValueError) as exc:
        return [Problem(key, f"{target.name} heartbeat unreadable ({type(exc).__name__})")]
    age = _heartbeat_age_s(target, payload, probes.now())
    if age is None:
        return [Problem(key, f"{target.name} heartbeat has no valid '{target.ts_field}'")]
    if age > settings.heartbeat_max_age_s:
        limit = settings.heartbeat_max_age_s
        return [Problem(key, f"{target.name} heartbeat is {age:.0f}s old (limit {limit:.0f}s)")]
    return []


_RECORDER_PREFIX = "recorder:"
# one JSON document with the container State and the RestartCount (the latter is not part of State)
_INSPECT_FORMAT = '{"state":{{json .State}},"restarts":{{.RestartCount}}}'


def _parse_inspect(stdout: str) -> tuple[object, int | None]:
    try:
        doc = json.loads(stdout)
    except ValueError:
        return None, None
    if not isinstance(doc, dict):
        return None, None
    restarts = doc.get("restarts")
    count = restarts if isinstance(restarts, int) and not isinstance(restarts, bool) else None
    return doc.get("state"), count


def _restart_note(target: Target, count: int | None, probes: Probes) -> str:
    """Crash-loop signal: the container's RestartCount went up since the last cycle.

    The comparison baseline (``restart_counts``) only advances once the resulting note is actually
    sent (``Monitor._commit_restart_count``) -- not merely because this check observed it -- so a
    Telegram back-off or the 8-messages-per-cycle cap cannot silently consume a restart tick. Until
    committed, the delta keeps being reported, cumulatively, cycle after cycle.
    """
    if count is None:
        return ""
    memory = probes.memory
    baseline = memory.restart_counts.get(target.container)
    if baseline is None:
        memory.restart_counts[target.container] = count  # bootstrap: nothing to compare yet, no note
        return ""
    memory.pending_restart_counts[target.container] = count
    if count <= baseline:
        return ""
    return f"{target.container} restarted {count - baseline} time(s) since the last check (total {count})"


def _inspect_target(target: Target, settings: Settings, probes: Probes) -> list[Problem]:
    res = probes.run(["docker", "inspect", "--format", _INSPECT_FORMAT, target.container])
    if res.returncode != 0:
        if "no such" in res.stderr.lower():
            return [Problem(recorder_key(target), f"{target.container} does not exist", confirm=True)]
        raise ToolError("docker", f"inspect failed: {short(res.stderr, 120)}")
    state, restarts = _parse_inspect(res.stdout)
    found = evaluate_container(target, state, probes.now()) or check_heartbeat(target, settings, probes)
    note = _restart_note(target, restarts, probes)
    if not note:
        return found
    if found:  # keep the existing problem's flags, add the restart evidence to its text
        first = found[0]
        return [Problem(first.key, f"{first.summary}; {note}", first.confirm), *found[1:]]
    return [Problem(recorder_key(target), note)]


def check_recorders(settings: Settings, probes: Probes) -> list[Problem]:
    """Classes 1 and 4 for every recorder target; a missing/broken docker is one ``tool:docker``.

    If docker itself cannot be queried, nothing is known about ANY recorder: the ``tool:docker`` problem
    freezes every ``recorder:*`` alert so none of them is reported as resolved.
    """
    problems: list[Problem] = []
    try:
        for target in settings.active_targets:
            problems.extend(_inspect_target(target, settings, probes))
    except ToolError as exc:
        return [Problem("tool:docker", f"docker unavailable: {exc.reason}", freeze=(_RECORDER_PREFIX,))]
    return problems


# ---------------------------------------------------------------------------------------------
# Checks: backups
# ---------------------------------------------------------------------------------------------


IN_PROGRESS_GRACE_HOURS = 1.0  # a run writes in_progress=true at the start; longer than this is stuck


def evaluate_lbank_status(
    raw: str | None, now: datetime, max_age_hours: float, *, error: str = ""
) -> list[Problem]:
    """Class 2 for the LBank backup status file (contract: see module docstring / task brief).

    ``raw`` is the file's text, or ``None`` with ``error`` naming why it could not be read.
    Alerts on: unreadable / corrupt file, ``ok=false``, ``integrity_ok=false``, a timestamp older
    than ``max_age_hours``, or ``in_progress=true`` for more than ``IN_PROGRESS_GRACE_HOURS`` (the
    writer sets this at the start of every run; still true after the grace period means the run was
    killed or is stuck -- see ``deploy/backup_lbank.py``). A missing ``in_progress`` (older status
    files, before that field existed) is treated as ``false``. A file that does not exist yet is NOT
    handled here (see ``check_lbank_backup``: it has a grace period).
    """
    key = "backup:lbank"
    if raw is None:
        return [Problem(f"{key}:unreadable", f"LBank backup status file unreadable ({error})")]
    try:
        data = json.loads(raw)
    except ValueError:
        return [Problem(key, "LBank backup status file is corrupt (invalid JSON)")]
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        return [Problem(key, "LBank backup status file is corrupt (missing 'ok')")]
    ts = parse_iso(data.get("ts"))
    if ts is None:
        return [Problem(key, "LBank backup status file is corrupt (bad 'ts')")]
    if data.get("in_progress") is True:
        started = parse_iso(data.get("started_ts")) or ts
        running_h = (now - started).total_seconds() / 3600.0
        if running_h <= IN_PROGRESS_GRACE_HOURS:
            # A normal run under way: not stuck yet. This must NOT be [] -- the writer's very first
            # write of a run sets in_progress=true/ok=false over whatever last night's status said,
            # so if last night's run had genuinely failed (an open "backup:lbank" alert), returning
            # no problems at all would read as "cleared" and RESOLVE it before the new run even
            # finishes. Freeze the key instead: neither resolved nor re-alerted until the run ends.
            summary = f"LBank backup run in progress (started {running_h * 60:.0f} min ago)"
            return [Problem(key, summary, freeze=(key,), silent=True)]
        summary = (
            f"LBank backup has been 'in_progress' for {running_h:.1f} h"
            f" (limit {IN_PROGRESS_GRACE_HOURS:g} h): the run was killed or is stuck"
        )
        return [Problem(key, summary)]
    issues: list[str] = []
    if data["ok"] is False:
        message = short(str(data.get("message", "")), 160)
        issues.append(f"backup failed (exit {data.get('exit_code')}): {message}")
    if data.get("integrity_ok") is False:
        issues.append("integrity check FAILED")
    age_h = (now - ts).total_seconds() / 3600.0
    if age_h > max_age_hours:
        issues.append(f"last backup status is {age_h:.0f} h old (limit {max_age_hours:g} h)")
    return [Problem(key, "LBank " + "; ".join(issues))] if issues else []


_LBANK_ANCHOR = "lbank_status"


def check_lbank_backup(settings: Settings, probes: Probes) -> list[Problem]:
    """Class 2 for LBank. Three distinct keys, so a real failure always raises a NEW alert:

    * ``backup:lbank``: the status file says failed / corrupt / stale,
    * ``backup:lbank:unreadable``: the file exists but cannot be read (permissions, symlink, too big),
    * ``backup:lbank:nostatus``: the file has not been created yet. This is normal right after the
      backup job is first deployed, so it is quiet for ``backup_max_age_hours`` counted from the last
      time a status was seen (or, if none ever was, from the first time it was found missing), and
      alerts after that: a backup that never reports must not pass silently.
    """
    now = probes.now()
    anchors = probes.memory.anchors
    try:
        raw = probes.read_text(settings.lbank_status_file)
    except FileNotFoundError:
        started = parse_iso(anchors.setdefault(_LBANK_ANCHOR, now.isoformat())) or now
        waited_h = (now - started).total_seconds() / 3600.0
        if waited_h <= settings.backup_max_age_hours:
            return []
        return [
            Problem(
                "backup:lbank:nostatus",
                f"LBank backup status file does not exist and none has appeared for {waited_h:.0f} h"
                f" (limit {settings.backup_max_age_hours:g} h): the nightly backup has never reported",
            )
        ]
    except OSError as exc:
        return evaluate_lbank_status(None, now, settings.backup_max_age_hours, error=type(exc).__name__)
    anchors[_LBANK_ANCHOR] = now.isoformat()
    return evaluate_lbank_status(raw, now, settings.backup_max_age_hours)


def check_trades_backup(settings: Settings, probes: Probes) -> list[Problem]:
    """Class 2 for the Tabdeal trades backup: newest ``trades-<UTC stamp>.sqlite`` must be recent."""
    key = "backup:trades"
    if key in settings.retired_keys:  # belongs to the retired Tabdeal recorder
        return []
    try:
        names = probes.list_names(settings.trades_backup_dir)
    except OSError as exc:
        return [Problem(key, f"trades backup dir unreadable ({type(exc).__name__})")]
    stamps = []
    for name in names:
        match = _TRADES_BACKUP_RE.match(name)
        if match:
            stamps.append(datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC))
    if not stamps:
        return [Problem(key, "no trades backup file found")]
    age_h = (probes.now() - max(stamps)).total_seconds() / 3600.0
    if age_h > settings.backup_max_age_hours:
        limit = settings.backup_max_age_hours
        return [Problem(key, f"newest trades backup is {age_h:.0f} h old (limit {limit:g} h)")]
    return []


_UNIT_CLEAR_STATES = frozenset(
    {"active", "inactive", "activating", "deactivating", "reloading", "maintenance"}
)


def check_units(settings: Settings, probes: Probes) -> list[Problem]:  # noqa: ARG001
    """Class 2: a backup systemd unit in the ``failed`` state.

    ``systemctl is-failed`` is trusted only when it answers with one of the known, documented unit
    states (``_UNIT_CLEAR_STATES``, or ``failed`` itself). Anything else -- non-zero exit with empty
    stdout, "Failed to connect to bus" (dbus unreachable), or any other unexpected text -- is
    inconclusive, not evidence the unit is healthy: it freezes that one unit's own key instead of
    being silently read as "not failed".
    """
    problems: list[Problem] = []
    try:
        for unit in BACKUP_UNITS:
            key = f"backup:unit:{unit}"
            state = probes.run(["systemctl", "is-failed", unit]).stdout.strip()
            if state == "failed":
                problems.append(Problem(key, f"systemd unit {unit} is failed"))
            elif state not in _UNIT_CLEAR_STATES:
                summary = f"systemctl is-failed {unit}: unexpected output {short(state, 80)!r}"
                problems.append(Problem(key, summary, freeze=(key,), silent=True))
    except ToolError as exc:
        return [Problem("tool:systemctl", f"systemctl unavailable: {exc.reason}", freeze=("backup:unit:",))]
    return problems


CheckFn = Callable[[Settings, Probes], list[Problem]]

CHECKS: tuple[tuple[str, CheckFn], ...] = (
    ("disks", check_disks),
    ("recorders", check_recorders),
    ("lbank_backup", check_lbank_backup),
    ("trades_backup", check_trades_backup),
    ("units", check_units),
)

# Alert keys each check owns. If the check crashes, its result is unknown, so these are frozen.
CHECK_KEY_PREFIXES: dict[str, tuple[str, ...]] = {
    "disks": ("disk:",),
    "recorders": (_RECORDER_PREFIX,),
    "lbank_backup": ("backup:lbank",),
    "trades_backup": ("backup:trades",),
    "units": ("backup:unit:",),
}


def collect(settings: Settings, probes: Probes, checks: Sequence[tuple[str, CheckFn]] = CHECKS) -> Collected:
    """Run every check. A crashing check becomes a ``monitor:<name>`` problem AND freezes its keys."""
    found: dict[str, Problem] = {}
    frozen: list[str] = []
    for name, check in checks:
        try:
            results = check(settings, probes)
        except Exception as exc:
            log.error("check %s crashed: %s", name, type(exc).__name__)
            summary = f"health check '{name}' crashed ({type(exc).__name__})"
            results = [Problem(f"monitor:{name}", summary, freeze=CHECK_KEY_PREFIXES.get(name, ()))]
        for problem in results:
            frozen.extend(problem.freeze)
            if not problem.silent:
                found.setdefault(problem.key, problem)
    return Collected(found, tuple(dict.fromkeys(frozen)))


def collect_problems(
    settings: Settings, probes: Probes, checks: Sequence[tuple[str, CheckFn]] = CHECKS
) -> dict[str, Problem]:
    """Current problems only (what ``--check`` prints)."""
    return collect(settings, probes, checks).problems


# ---------------------------------------------------------------------------------------------
# Telegram (GET only, send-only)
# ---------------------------------------------------------------------------------------------


class TelegramError(Exception):
    """A Telegram call failed. ``reason`` is a short fixed phrase; the URL is never included."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason if status is None else f"{reason} (HTTP {status})")
        self.reason = reason
        self.status = status


def telegram_get(
    token: str,
    method: str,
    params: Mapping[str, str],
    *,
    base_url: str = TELEGRAM_BASE_URL,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Call a Telegram Bot API method with HTTP GET and return the decoded ``result`` envelope.

    Every failure is re-raised as ``TelegramError`` built from a fixed phrase and the HTTP status
    only: urllib/httpx exceptions carry the request URL (which contains the token) and are
    therefore never chained (``from None``) or formatted.
    """
    query = urllib.parse.urlencode(params)
    url = f"{base_url}/bot{token}/{method}?{query}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        exc.close()
        raise TelegramError("http error", code) from None
    except TimeoutError:
        raise TelegramError("timeout") from None
    except urllib.error.URLError as exc:
        timed_out = isinstance(exc.reason, TimeoutError)
        raise TelegramError("timeout" if timed_out else "connect error") from None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise TelegramError(f"request failed: {type(exc).__name__}") from None
    try:
        envelope = json.loads(body)
    except ValueError:
        raise TelegramError("malformed response", status) from None
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        raise TelegramError("telegram did not accept the request", status)
    return envelope


class TelegramSender:
    """Sends one text message to the configured chat. Token and chat id are masked in ``repr``."""

    def __init__(
        self, token: str, chat_id: str, *, base_url: str = TELEGRAM_BASE_URL, timeout: float = 10.0
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._base_url = base_url
        self._timeout = timeout
        register_secret(token)

    def __call__(self, text: str) -> None:
        params = {"chat_id": self._chat_id, "text": text[:_MAX_TEXT_CHARS], "disable_notification": "false"}
        telegram_get(self._token, "sendMessage", params, base_url=self._base_url, timeout=self._timeout)

    def __repr__(self) -> str:
        return "TelegramSender(token=***, chat_id=***)"


# ---------------------------------------------------------------------------------------------
# State (dedupe) and the monitor loop
# ---------------------------------------------------------------------------------------------


@dataclass
class Tracked:
    first_ts: str
    last_sent_ts: str
    summary: str
    clear_count: int = 0


@dataclass
class MonitorState:
    tracked: dict[str, Tracked] = field(default_factory=dict)
    last_start_notice_ts: str | None = None
    anchors: dict[str, str] = field(default_factory=dict)
    restart_counts: dict[str, int] = field(default_factory=dict)


class StateStore:
    """Small JSON file; written atomically (temp file + fsync + rename)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> MonitorState:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            tracked = {
                str(key): Tracked(
                    first_ts=str(item["first_ts"]),
                    last_sent_ts=str(item["last_sent_ts"]),
                    summary=str(item["summary"]),
                    clear_count=int(item.get("clear_count", 0)),
                )
                for key, item in data["tracked"].items()
            }
            notice = data.get("last_start_notice_ts")
            anchors = {str(k): str(v) for k, v in (data.get("anchors") or {}).items()}
            restarts = {str(k): int(v) for k, v in (data.get("restart_counts") or {}).items()}
            return MonitorState(tracked, str(notice) if notice else None, anchors, restarts)
        except FileNotFoundError:
            return MonitorState()
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.error("state file unreadable, starting empty (%s)", type(exc).__name__)
            return MonitorState()

    def save(self, state: MonitorState) -> None:
        payload = {
            "version": 1,
            "last_start_notice_ts": state.last_start_notice_ts,
            "anchors": state.anchors,
            "restart_counts": state.restart_counts,
            "tracked": {
                key: {
                    "first_ts": t.first_ts,
                    "last_sent_ts": t.last_sent_ts,
                    "summary": t.summary,
                    "clear_count": t.clear_count,
                }
                for key, t in state.tracked.items()
            },
        }
        tmp_name: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self._path.parent, prefix=".state-", suffix=".tmp", delete=False
            ) as handle:
                tmp_name = handle.name
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp_name).replace(self._path)
        except OSError as exc:
            log.error("cannot write state file (%s)", type(exc).__name__)
            if tmp_name is not None:
                with contextlib.suppress(OSError):
                    Path(tmp_name).unlink()


@dataclass(frozen=True)
class Event:
    kind: str  # "alert" | "reminder" | "resolved"
    key: str
    summary: str


Sender = Callable[[str], None]


class Monitor:
    """One cycle = run checks, work out what changed, send what is due, persist state."""

    def __init__(self, settings: Settings, probes: Probes, send: Sender, store: StateStore) -> None:
        self._s = settings
        self._probes = probes
        self._send = send
        self._store = store
        self._state = store.load()
        self._memory = probes.memory
        self._memory.anchors.update(self._state.anchors)
        self._memory.restart_counts.update(self._state.restart_counts)
        self._pending: dict[str, int] = {}  # consecutive bad cycles of not-yet-alerting keys
        self._send_failures = 0
        self._next_send_at: datetime | None = None

    # -- formatting -------------------------------------------------------------------------

    def _text(self, label: str, key: str | None, summary: str, now: datetime) -> str:
        head = f"[{self._s.hostname}] {label}" + (f" {key}" if key else "")
        return redact(f"{head}\n{short(summary, 400)}\n{now:%Y-%m-%d %H:%M:%S} UTC")

    # -- sending with backoff ---------------------------------------------------------------

    def _try_send(self, text: str, now: datetime) -> bool:
        if self._next_send_at is not None and now < self._next_send_at:
            return False
        try:
            self._send(text)
        except TelegramError as exc:
            self._send_failures += 1
            delay = min(self._s.interval_s * (2 ** (self._send_failures - 1)), _MAX_BACKOFF_S)
            self._next_send_at = now + timedelta(seconds=delay)
            log.error("telegram send failed: %s (retry in %.0fs)", exc, delay)
            return False
        except Exception as exc:  # a custom/injected sender must never crash the loop either
            self._send_failures += 1
            self._next_send_at = now + timedelta(seconds=min(self._s.interval_s, _MAX_BACKOFF_S))
            log.error("telegram send failed: %s", type(exc).__name__)
            return False
        self._send_failures = 0
        self._next_send_at = None
        return True

    def start(self) -> None:
        """Close alerts of retired targets, then send the one "monitor started" message (suppressed
        during a rapid restart loop). The retired targets are always logged, never silently hidden."""
        now = self._probes.now()
        if self._s.retired:
            log.info("monitor starting; retired (not monitored): %s", ", ".join(self._s.retired))
        self._close_retired(now)
        last = parse_iso(self._state.last_start_notice_ts)
        if last is not None and (now - last).total_seconds() < _START_NOTICE_MIN_GAP_S:
            log.info("start notice suppressed (sent %.0fs ago)", (now - last).total_seconds())
            return
        detail = f"{len(CHECKS)} checks every {self._s.interval_s:g}s"
        if self._s.retired:
            detail += f"; RETIRED (not monitored): {', '.join(self._s.retired)}"
        if self._try_send(self._text("INFO monitor started", None, detail, now), now):
            self._state.last_start_notice_ts = now.isoformat()
            self._store.save(self._state)

    # -- one cycle --------------------------------------------------------------------------

    def _confirmed(self, problem: Problem) -> bool:
        """Flapping guard: a ``confirm`` problem must be seen ``confirm_cycles`` cycles in a row."""
        if not problem.confirm or self._s.confirm_cycles <= 1:
            return True
        seen = self._pending.get(problem.key, 0) + 1
        self._pending[problem.key] = seen
        return seen >= self._s.confirm_cycles

    def _close_retired(self, now: datetime) -> None:
        """Close every OPEN alert whose key belongs to a retired target with ONE "RETIRED" message.

        Never "RESOLVED": the problem was not resolved, the target is simply no longer watched. The
        entry is dropped from the persisted state only once the message was really sent, so a
        Telegram outage delays it (retried next cycle) but never loses it; afterwards nothing is
        left to repeat.
        """
        retired_keys = self._s.retired_keys
        for key in sorted(k for k in self._state.tracked if k in retired_keys):
            text = redact(f"[{self._s.hostname}] RETIRED {key}: no longer monitored ({ENV_RETIRED})")
            text += f"\n{now:%Y-%m-%d %H:%M:%S} UTC"
            if not self._try_send(text, now):
                return
            del self._state.tracked[key]
            self._pending.pop(key, None)
            self._store.save(self._state)

    def _plan(self, found: Collected, now: datetime) -> list[Event]:
        problems = found.problems
        tracked = self._state.tracked
        retired_keys = self._s.retired_keys
        repeat = timedelta(hours=self._s.repeat_hours)
        events: list[Event] = []
        for key in [k for k in self._pending if k not in problems]:
            del self._pending[key]  # a bad streak is broken by a good OR an unknown cycle
        for key, problem in problems.items():
            item = tracked.get(key)
            if item is None:
                if self._confirmed(problem):
                    events.append(Event("alert", key, problem.summary))
                continue
            item.summary, item.clear_count = problem.summary, 0
            last_sent = parse_iso(item.last_sent_ts) or now
            if now - last_sent >= repeat:
                events.append(Event("reminder", key, problem.summary))
        for key, item in tracked.items():
            if key in problems or key in retired_keys:  # retired: closed by _close_retired, not resolved
                continue
            if found.is_frozen(key):  # unknown is not clear: keep the alert open, restart the count
                item.clear_count = 0
                continue
            item.clear_count += 1
            if item.clear_count >= self._s.resolve_cycles:
                events.append(Event("resolved", key, item.summary))
        return events

    def _apply(self, event: Event, now: datetime) -> None:
        tracked = self._state.tracked
        if event.kind == "resolved":
            tracked.pop(event.key, None)
        elif event.kind == "alert":
            tracked[event.key] = Tracked(now.isoformat(), now.isoformat(), event.summary)
        else:
            tracked[event.key].last_sent_ts = now.isoformat()

    def _commit_restart_count(self, key: str) -> None:
        """Once a recorder's message has actually been sent, the restart-count note in it has been
        delivered: advance the comparison baseline so the same tick is not reported again. Until
        this runs, ``_restart_note`` keeps comparing against the OLD baseline (see its docstring)."""
        if not key.startswith(_RECORDER_PREFIX):
            return
        for target in self._s.targets:
            if recorder_key(target) == key:
                pending = self._memory.pending_restart_counts.pop(target.container, None)
                if pending is not None:
                    self._memory.restart_counts[target.container] = pending
                return

    def _message(self, event: Event, now: datetime) -> str:
        if event.kind == "resolved":
            item = self._state.tracked.get(event.key)
            first = parse_iso(item.first_ts) if item else None
            span = f" after {(now - first).total_seconds() / 60:.0f} min" if first else ""
            return self._text("RESOLVED", event.key, f"{event.summary}{span}", now)
        return self._text("ALERT" if event.kind == "alert" else "REMINDER", event.key, event.summary, now)

    def run_cycle(self) -> list[Event]:
        """Run all checks once. Returns the events that were actually sent."""
        now = self._probes.now()
        self._close_retired(now)  # normally done in start(); this retries after a Telegram outage
        self._memory.open_keys = set(self._state.tracked)
        found = collect(self._s, self._probes)
        sent: list[Event] = []
        for event in self._plan(found, now)[:_MAX_EVENTS_PER_CYCLE]:
            if not self._try_send(self._message(event, now), now):
                break
            self._apply(event, now)
            self._commit_restart_count(event.key)
            sent.append(event)
        self._state.anchors = dict(self._memory.anchors)
        self._state.restart_counts = dict(self._memory.restart_counts)
        self._store.save(self._state)
        log.info("cycle done: %d problem(s), %d message(s) sent", len(found.problems), len(sent))
        return sent


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


class DeadmanPinger:
    """Optional dead-man switch (healthchecks.io style): one HTTP GET to a URL you own, made after
    every completed cycle. If the whole server (and with it this monitor) dies, the service on the
    other end notices the missing pings and tells you -- nothing on this host can do that.

    The URL is a secret: it is never logged or printed (``repr`` is masked, failures are logged as
    an exception type only) and every failure is ignored.
    """

    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        self._url = url
        self._timeout = timeout
        self._failing = False
        register_secret(url)

    def __call__(self) -> bool:
        try:
            request = urllib.request.Request(self._url, method="GET")
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                response.read(1024)
        except Exception as exc:  # urllib errors can carry the URL: log the type only
            if not self._failing:
                log.warning("dead-man ping failed (%s); will keep trying silently", type(exc).__name__)
            self._failing = True
            return False
        if self._failing:
            log.info("dead-man ping works again")
        self._failing = False
        return True

    def __repr__(self) -> str:
        return "DeadmanPinger(url=***)"


def run_forever(
    monitor: Monitor,
    interval_s: float,
    stop: threading.Event,
    ping: Callable[[], object] | None = None,
) -> None:
    while not stop.is_set():
        try:
            monitor.run_cycle()
        except Exception as exc:  # last line of defence: the loop must survive anything
            log.error("monitor cycle crashed: %s", type(exc).__name__)
        else:
            if ping is not None:
                try:
                    ping()
                except Exception as exc:  # a broken ping must never stop the monitor
                    log.error("dead-man ping crashed: %s", type(exc).__name__)
        stop.wait(interval_s)


def _print_problems(problems: Mapping[str, Problem]) -> None:
    if not problems:
        print("no problems found")
    for problem in problems.values():
        print(f"{problem.key}: {problem.summary}")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tbot Telegram health-alert monitor (send-only)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="run checks once, print problems, send nothing")
    mode.add_argument("--send-test", action="store_true", help="send one test message to Telegram and exit")
    args = parser.parse_args(argv)
    environ = os.environ if env is None else env
    setup_logging()
    try:
        settings = load_settings(environ)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    probes = Probes()
    if args.check:
        _print_problems(collect_problems(settings, probes))
        return 0
    try:
        token, chat_id = load_credentials(environ)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    sender = TelegramSender(token, chat_id)
    if args.send_test:
        return _send_test(sender, settings, probes)
    monitor = Monitor(settings, probes, sender, StateStore(settings.state_dir / "state.json"))
    stop = threading.Event()

    def _handle(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    monitor.start()
    ping = DeadmanPinger(settings.deadman_url) if settings.deadman_url else None
    run_forever(monitor, settings.interval_s, stop, ping)
    return 0


def _send_test(sender: Sender, settings: Settings, probes: Probes) -> int:
    stamp = f"{probes.now():%Y-%m-%d %H:%M:%S} UTC"
    text = f"[{settings.hostname}] TEST health monitor can reach Telegram\n{stamp}"
    try:
        sender(text)
    except TelegramError as exc:
        print(f"test message FAILED: {exc}", file=sys.stderr)
        return 1
    print("test message sent")
    return 0
