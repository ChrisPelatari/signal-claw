#!/usr/bin/env python3
"""
signal-claw: a Signal -> Claude bridge.

Runs one `signal-cli jsonRpc --receive-mode=on-start` child process, then
multiplexes incoming-message notifications and outgoing-send requests over
that single process. This avoids signal-cli's per-account database lock,
which prevents a second send invocation while a receive is active.

Routing rules (both homeline DMs and note-to-self share these):
  - Body normalized to 'pulse-agents' (bare OR after prefix strip)
        -> render local dashboard, reply directly (no claude spawn).
  - Body starts with '<TRIGGER_WORD>@<HOSTNAME>' (case-insensitive, optional
    ':' / ',' / '-' / whitespace separator)
        -> strip prefix, wake claude, reply to the source channel.
  - Everything else -> log and drop silently.

The prefix gate exists so one Signal account can be linked to many machines
without every machine answering every message. Each host only responds to
messages explicitly addressed to it. `pulse-agents` is the deliberate
exception: fleet-wide pings answered by every relay simultaneously.

All configuration is taken from environment variables (see config.example.env).
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from queue import Queue, Empty


def env(key: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        sys.stderr.write(f"signal-claw: missing required env var {key}\n")
        sys.exit(2)
    return val or ""


ACCOUNT        = env("SIGNAL_ACCOUNT", required=True)
HOMELINE       = env("SIGNAL_HOMELINE", required=True)
TRIGGER_WORD   = env("TRIGGER_WORD", "claude").lower()
HOSTNAME       = env("SIGNAL_HOSTNAME", socket.gethostname().split(".", 1)[0]).strip().lower()
SIGNAL_CLI     = env("SIGNAL_CLI", "/usr/bin/signal-cli")
CLAUDE         = env("CLAUDE", "/usr/bin/claude")
STATE_DIR      = Path(env("STATE_DIR", str(Path.home() / ".local/share/signal-claude")))
LOG_FILE       = Path(env("LOG_FILE", str(STATE_DIR / "daemon.log")))
CLAUDE_TIMEOUT = int(env("CLAUDE_TIMEOUT", "240"))
SIGNAL_RETRY   = int(env("SIGNAL_RETRY", "5"))
MAX_REPLY_LEN  = int(env("MAX_REPLY_LEN", "3800"))
SESSIONS_FILE  = Path(env("SESSIONS_FILE", str(STATE_DIR / "sessions.json")))

PREFIX_RE = re.compile(
    rf"^{re.escape(TRIGGER_WORD)}@{re.escape(HOSTNAME)}\b[\s:,\-]*",
    re.IGNORECASE,
)
PULSE_TRIGGER = "pulse-agents"

STATE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("signal-claw")


class SignalRpc:
    """One signal-cli jsonRpc subprocess, multiplexed for send + receive."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.next_id = 1
        self.send_lock = threading.Lock()
        self.events: Queue[dict] = Queue()

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [SIGNAL_CLI, "-a", ACCOUNT, "jsonRpc", "--receive-mode=on-start"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr_reader, daemon=True).start()
        log.info("signal-cli jsonRpc started pid=%s", self.proc.pid)

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("non-json stdout: %s", line[:200])
                continue
            if msg.get("method") == "receive":
                self.events.put(msg.get("params") or {})
            elif "id" in msg and "error" in msg:
                log.error("signal-cli rpc error id=%s err=%s", msg.get("id"), msg["error"])

    def _stderr_reader(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            line = line.rstrip()
            if line:
                log.info("[signal-cli] %s", line[-400:])

    def send(self, *, recipient: str | None = None, note_to_self: bool = False,
             message: str) -> None:
        if not self.proc or not self.proc.stdin:
            log.error("send called before start()")
            return
        with self.send_lock:
            req_id = self.next_id
            self.next_id += 1
            params: dict = {"message": message}
            if note_to_self:
                params["noteToSelf"] = True
            else:
                params["recipient"] = [recipient]
            req = {"jsonrpc": "2.0", "method": "send", "params": params, "id": req_id}
            try:
                self.proc.stdin.write(json.dumps(req) + "\n")
                self.proc.stdin.flush()
            except BrokenPipeError:
                log.error("send: broken pipe to signal-cli")


def extract(envelope: dict) -> tuple[str | None, str | None, bool]:
    """Return (reply_target, body, is_note_to_self) or (None, None, False) to skip."""
    src = envelope.get("source") or envelope.get("sourceNumber")

    data_msg = envelope.get("dataMessage") or {}
    sync_sent = ((envelope.get("syncMessage") or {}).get("sentMessage")) or {}

    if data_msg.get("message") and src == HOMELINE:
        return HOMELINE, data_msg["message"], False

    if sync_sent.get("message"):
        dest = sync_sent.get("destination") or sync_sent.get("destinationNumber")
        if dest == ACCOUNT:
            return ACCOUNT, sync_sent["message"], True

    return None, None, False


def match_prefix(body: str) -> tuple[bool, str]:
    """Return (matched, body-with-prefix-removed). Empty residue is allowed."""
    s = body.lstrip()
    m = PREFIX_RE.match(s)
    if not m:
        return False, body
    return True, s[m.end():]


def normalize_trigger(text: str) -> str:
    """Collapse whitespace + lowercase so 'Pulse Agents' == 'pulse-agents'... almost.

    We strip whitespace entirely, so 'PULSE-AGENTS', 'pulse  agents', and 'Pulse-Agents'
    all collapse to 'pulse-agents'. Hyphens are preserved.
    """
    return "".join(text.lower().split())


def render_pulse() -> str:
    """Return a one-line dashboard for fleet-wide 'pulse-agents' pings.

    Reads /proc directly to avoid forking df/uptime/free. Falls back to '?' on
    any parse error so we never fail to reply.
    """
    host = socket.gethostname().split(".", 1)[0]

    def _read(path: str) -> str:
        try:
            return Path(path).read_text()
        except OSError:
            return ""

    try:
        up_s = float(_read("/proc/uptime").split()[0])
        d, rem = divmod(int(up_s), 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        uptime = f"{d}d{h:02d}h{m:02d}m" if d else f"{h}h{m:02d}m"
    except (ValueError, IndexError):
        uptime = "?"

    load_parts = _read("/proc/loadavg").split()
    load = " ".join(load_parts[:3]) if len(load_parts) >= 3 else "?"

    try:
        meminfo: dict[str, int] = {}
        for line in _read("/proc/meminfo").splitlines():
            key, _, rest = line.partition(":")
            if rest:
                meminfo[key.strip()] = int(rest.strip().split()[0])
        total_g = meminfo["MemTotal"] / 1024 / 1024
        avail_g = meminfo.get("MemAvailable", meminfo["MemTotal"]) / 1024 / 1024
        mem = f"{total_g - avail_g:.1f}/{total_g:.1f}G"
    except (KeyError, ValueError):
        mem = "?"

    try:
        st = os.statvfs("/")
        total_g = st.f_blocks * st.f_frsize / 1024**3
        free_g  = st.f_bavail * st.f_frsize / 1024**3
        used_g  = total_g - free_g
        pct = int(round(used_g / total_g * 100)) if total_g else 0
        disk = f"{used_g:.0f}/{total_g:.0f}G ({pct}%)"
    except OSError:
        disk = "?"

    return f"{host} · up {uptime} · load {load} · mem {mem} · root {disk}"


def fast_path_pulse_agents(body: str, target: str, nts: bool, rpc: "SignalRpc") -> bool:
    """If body == 'pulse-agents' (normalized), render+send and return True."""
    if normalize_trigger(body) != PULSE_TRIGGER:
        return False
    try:
        reply = render_pulse()
    except Exception as e:
        log.exception("pulse render failed")
        reply = f"({HOSTNAME}: pulse render failed: {e})"
    if nts:
        rpc.send(note_to_self=True, message=reply)
    else:
        rpc.send(recipient=target, message=reply)
    log.info("fast-path pulse-agents -> %s", "note-to-self" if nts else target)
    return True


_sessions_lock = threading.Lock()


def load_sessions() -> dict[str, str]:
    try:
        with SESSIONS_FILE.open() as f:
            data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(v, str)}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("sessions file unreadable, starting fresh: %s", e)
        return {}


def save_sessions(sessions: dict[str, str]) -> None:
    try:
        tmp = SESSIONS_FILE.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(sessions, f, indent=2, sort_keys=True)
        tmp.replace(SESSIONS_FILE)
    except OSError as e:
        log.error("failed to save sessions: %s", e)


def _invoke_claude(args: list[str], prompt: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [CLAUDE, "-p", prompt, *args],
        capture_output=True, text=True,
        timeout=CLAUDE_TIMEOUT,
        cwd=str(Path.home()),
    )


def run_claude(prompt: str, channel: str) -> str:
    """Invoke claude with persistent per-channel session memory.

    First message in a channel: --session-id <new uuid> (creates the session).
    Subsequent messages: --resume <uuid> (continues the same conversation).
    If --resume fails (session deleted/expired), recover by minting a fresh UUID.
    """
    with _sessions_lock:
        sessions = load_sessions()
        existing = sessions.get(channel)

    try:
        if existing:
            proc = _invoke_claude(["--resume", existing], prompt)
            if proc.returncode != 0 and ("not found" in (proc.stderr or "").lower()
                                          or "no such session" in (proc.stderr or "").lower()):
                log.warning("session %s lost for channel=%s, restarting", existing, channel)
                existing = None  # fall through to creation path
        if not existing:
            new_id = str(uuid.uuid4())
            proc = _invoke_claude(["--session-id", new_id], prompt)
            with _sessions_lock:
                sessions = load_sessions()
                sessions[channel] = new_id
                save_sessions(sessions)

        out = (proc.stdout or "").strip()
        if not out:
            out = (proc.stderr or "").strip() or "(claude returned no output)"
        return out
    except subprocess.TimeoutExpired:
        return f"(claude timed out after {CLAUDE_TIMEOUT}s)"
    except Exception as e:
        return f"(claude error: {e})"


def truncate(text: str, n: int = MAX_REPLY_LEN) -> str:
    if len(text) <= n:
        return text
    return text[: n - 30] + "\n…[truncated]"


def handle(params: dict, rpc: SignalRpc) -> None:
    envelope = params.get("envelope") or {}
    target, body, nts = extract(envelope)
    if not body or not target:
        return

    src = envelope.get("source")

    if fast_path_pulse_agents(body, target, nts, rpc):
        return

    matched, stripped = match_prefix(body)
    if not matched:
        log.info("dropped (no %s@%s prefix) src=%s body=%r",
                 TRIGGER_WORD, HOSTNAME, src, body[:80])
        return

    if fast_path_pulse_agents(stripped, target, nts, rpc):
        return

    prompt = stripped.strip()
    if not prompt:
        log.info("dropped (empty after prefix strip) src=%s", src)
        return

    channel = "nts" if nts else f"homeline:{target}"
    log.info("prompt src=%s channel=%s body=%r", src, channel, prompt[:200])
    reply = truncate(run_claude(prompt, channel))
    log.info("reply len=%d -> %s", len(reply), "note-to-self" if nts else target)

    if nts:
        rpc.send(note_to_self=True, message=reply)
    else:
        rpc.send(recipient=target, message=reply)


def main() -> None:
    log.info("=== signal-claw starting ===")
    while True:
        rpc = SignalRpc()
        try:
            rpc.start()
        except Exception:
            log.exception("failed to spawn signal-cli")
            time.sleep(SIGNAL_RETRY)
            continue

        try:
            while True:
                try:
                    params = rpc.events.get(timeout=5)
                except Empty:
                    if rpc.proc and rpc.proc.poll() is not None:
                        log.warning("signal-cli exited code=%s", rpc.proc.returncode)
                        break
                    continue
                try:
                    handle(params, rpc)
                except Exception:
                    log.exception("handler crashed")
        finally:
            try:
                if rpc.proc and rpc.proc.poll() is None:
                    rpc.proc.terminate()
                    rpc.proc.wait(timeout=5)
            except Exception:
                pass

        time.sleep(SIGNAL_RETRY)


if __name__ == "__main__":
    main()
