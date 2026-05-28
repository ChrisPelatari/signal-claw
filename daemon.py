#!/usr/bin/env python3
"""
signal-claw: a Signal -> Claude bridge.

Runs one `signal-cli jsonRpc --receive-mode=on-start` child process, then
multiplexes incoming-message notifications and outgoing-send requests over
that single process. This avoids signal-cli's per-account database lock,
which prevents a second send invocation while a receive is active.

Routing rules:
  - Direct message from $SIGNAL_HOMELINE                    -> wake claude, reply to home line.
  - Note-to-self whose body starts with $TRIGGER_WORD        -> wake claude, reply via note-to-self.
  - Everything else                                          -> log and ignore.

All configuration is taken from environment variables (see config.example.env).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
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
SIGNAL_CLI     = env("SIGNAL_CLI", "/usr/bin/signal-cli")
CLAUDE         = env("CLAUDE", "/usr/bin/claude")
STATE_DIR      = Path(env("STATE_DIR", str(Path.home() / ".local/share/signal-claude")))
LOG_FILE       = Path(env("LOG_FILE", str(STATE_DIR / "daemon.log")))
CLAUDE_TIMEOUT = int(env("CLAUDE_TIMEOUT", "240"))
SIGNAL_RETRY   = int(env("SIGNAL_RETRY", "5"))
MAX_REPLY_LEN  = int(env("MAX_REPLY_LEN", "3800"))

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


def strip_trigger(body: str) -> str:
    s = body.lstrip()
    low = s.lower()
    for prefix in (f"@{TRIGGER_WORD}", f"{TRIGGER_WORD}:", f"{TRIGGER_WORD},", TRIGGER_WORD):
        if low.startswith(prefix):
            return s[len(prefix):].lstrip(" :,-")
    return body


def has_trigger(body: str) -> bool:
    return body.lstrip().lower().startswith(TRIGGER_WORD) or \
           body.lstrip().lower().startswith(f"@{TRIGGER_WORD}")


def run_claude(prompt: str) -> str:
    try:
        proc = subprocess.run(
            [CLAUDE, "-p", prompt],
            capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT,
            cwd=str(Path.home()),
        )
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

    if nts and not has_trigger(body):
        return

    prompt = strip_trigger(body) if nts else body
    if not prompt.strip():
        log.info("empty prompt after trigger strip nts=%s", nts)
        return

    src = envelope.get("source")
    log.info("prompt src=%s nts=%s body=%r", src, nts, prompt[:200])
    reply = truncate(run_claude(prompt))
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
