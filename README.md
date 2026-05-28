# signal-claw

A small daemon that bridges Signal Messenger to [Claude Code](https://github.com/anthropics/claude-code). Text the linked Signal account from a trusted phone number (or as a Note-to-Self with a trigger word) and the daemon shells out to `claude -p`, replying with the result.

Linked-device setup, no phone-number registration. One Python process, one `signal-cli jsonRpc` child, no extra system services required — runs cleanly on machines without systemd via `@reboot` in crontab.

## How it works

```
  Your phone ──Signal──▶ Signal server ──▶ signal-cli (linked device)
                                                │
                                                ▼ JSON-RPC stdout
                                        ┌──────────────┐
                                        │ daemon.py    │
                                        │              │
                                        │  rules:      │
                                        │  • homeline  │
                                        │  • note-to-  │
                                        │    self with │
                                        │    trigger   │
                                        └──────┬───────┘
                                               │ spawn
                                               ▼
                                          claude -p <prompt>
                                               │ stdout
                                               ▼
                                        JSON-RPC stdin
                                          back to signal-cli
                                               │
                                               ▼
                                          reply over Signal
```

Single signal-cli child process — bidirectional stdio JSON-RPC — sidesteps signal-cli's per-account database lock that prevents a second `send` invocation while a `receive` is active.

## Routing rules

| Incoming                                              | Action                                |
|-------------------------------------------------------|---------------------------------------|
| Direct message from `$SIGNAL_HOMELINE`                | `claude -p $body` → reply to homeline |
| Note-to-self starting with `$TRIGGER_WORD`            | `claude -p $body` → reply via NtS     |
| Anything else                                         | log and ignore                        |

The trigger word on note-to-self prevents the daemon from replying to every personal note you make.

## Prerequisites

- Python 3.10+ (uses `subprocess`, `threading`, `json` — stdlib only)
- [signal-cli](https://github.com/AsamK/signal-cli) (tested with 0.14.3 — note that 0.14.4.1 has a known send bug)
- [Claude Code](https://github.com/anthropics/claude-code) CLI on `PATH`
- A working signal-cli link to your Signal account (see signal-cli's `link` command)

## Install

```bash
git clone <this-repo> ~/Documents/GitHub/signal-claw
cd ~/Documents/GitHub/signal-claw
cp config.example.env config.env
$EDITOR config.env        # set SIGNAL_ACCOUNT, SIGNAL_HOMELINE, paths
chmod +x start.sh daemon.py

# Test interactively first:
./start.sh                # logs to $STATE_DIR/cron.log + daemon.log

# Persist across reboots (machines without systemd):
( crontab -l 2>/dev/null; echo "@reboot $(pwd)/start.sh" ) | crontab -

# Or under systemd, create a user unit that ExecStart=/path/to/start.sh.
```

Linking signal-cli to your Signal account is a one-time step done **outside** this daemon:

```bash
signal-cli link -n "claude@$(hostname)"   # prints sgnl:// URI
# qrencode the URI, scan from Signal app → Settings → Linked Devices
```

## Operation

- Logs: `$STATE_DIR/daemon.log` (handler activity), `$STATE_DIR/cron.log` (startup script)
- signal-cli account state: `$STATE_DIR/signal-cli/` (managed by signal-cli)
- Restart: `pkill -f signal-claw/daemon.py` then re-run `start.sh`
- Stop permanently: also `crontab -e` to remove the `@reboot` line

## Security notes

- `config.env` contains phone numbers — keep it gitignored. `config.example.env` ships with placeholder values.
- The daemon shells out to `claude -p` with the *raw message body* as the prompt. Claude Code's tool permissions still apply, so the blast radius is whatever Claude Code is allowed to do on this machine. Don't expose the homeline channel to untrusted senders.
- signal-cli's account DB on disk is plaintext but only readable by your user. Standard Unix file permissions apply.

## License

MIT — see [LICENSE](LICENSE).
