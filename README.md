# request-reviewer

**Let a local LLM review Claude Code permission prompts, so you don't have to.**

Claude Code stops and asks for permission before running commands, editing
files, or fetching URLs. `request-reviewer` is a
[PreToolUse hook](https://code.claude.com/docs/en/hooks) that sends each
permission request to a small model running locally via
[Ollama](https://ollama.com). Safe requests are approved automatically;
dangerous or ambiguous ones still surface the normal prompt — now annotated
with the reviewer's reason.

- **Zero API tokens** — the reviewer runs entirely offline on your machine.
- **Zero dependencies** — one Python-stdlib script, one settings entry.
- **Fail-safe** — if Ollama is down, times out, or errors, the hook emits no
  decision and Claude Code falls back to asking you, like it always did.
- **Auditable** — every decision is logged to `~/.claude/request-reviewer.log`.

## Setup

Requires Python 3 and [Ollama](https://ollama.com/download).

1. Install Ollama and make sure it's running:
   ```sh
   brew install ollama && brew services start ollama   # macOS
   # Linux: curl -fsSL https://ollama.com/install.sh | sh
   ```
2. Clone this repo and run the installer:
   ```sh
   git clone https://github.com/ryanmcl0/request-reviewer
   cd request-reviewer
   ./install.sh
   ```
   This pulls the default model, copies the hook to
   `~/.claude/hooks/request-reviewer.py`, registers it in
   `~/.claude/settings.json` (backing up the old file first), and runs a
   smoke test.
3. Restart any running Claude Code session (`/hooks` should now list the
   PreToolUse hook). That's it — watch `~/.claude/request-reviewer.log` to
   see decisions happening.

To remove everything later: `./install.sh --uninstall`

### Setup via your AI agent

Paste this into Claude Code (or any coding agent) to have it set everything
up for you:

> Set up https://github.com/ryanmcl0/request-reviewer on this machine. Steps:
> (1) Ensure Ollama is installed and the server responds at
> http://localhost:11434/api/version — on macOS `brew install ollama && brew
> services start ollama`, on Linux use the install script from ollama.com.
> (2) Clone the repo and run `./install.sh` from its root; it pulls the model,
> installs the hook to ~/.claude/hooks/request-reviewer.py, and edits
> ~/.claude/settings.json itself — don't edit settings manually and don't
> reimplement the installer. (3) Confirm the installer's smoke test printed
> `"permissionDecision": "allow"`. (4) Run `python3 eval.py` and report the
> score; if cases fail, retry with a bigger model: `./install.sh qwen3.5:4b`.
> (5) Tell me the model installed, eval score, and average decision latency.
> The hook fails safe (no decision → normal permission prompt), so nothing
> here can lock me out.

## How it works

```
Claude Code wants to run a tool
        │
        ▼
PreToolUse hook (reviewer.py)
        │
        ├─ Tier 1: deterministic rules (microseconds)
        │    ├─ read-only command allowlist  → allow
        │    └─ hard blocklist → ask/deny (sudo, rm -rf ~, curl|sh, force push,
        │       git reset --hard / clean -f, ~/.ssh — never left to the model)
        │
        └─ Tier 2: local model via Ollama (~1s warm)
             structured output: {"decision": "allow" | "deny" | "unsure", "reason": "..."}
                  │
                  ├─ allow  → request auto-approved, Claude keeps working
                  ├─ unsure → normal permission prompt, with the model's reason
                  └─ deny   → prompt (default) or hard block (REVIEWER_ON_DENY=deny)
```

Anything you've already allowed with Claude Code permission rules never
reaches the hook, so keep using `/permissions` allowlists as the zero-latency
first line — the reviewer handles the long tail.

## Choosing a model

Any Ollama model that supports structured output works. Test candidates with
the included eval before trusting one:

```sh
REVIEWER_MODEL=some-model:tag python3 eval.py
```

Measured on an M2 Pro against the 18-case eval (avg includes instant
fast-path decisions; pure model decisions are roughly 2× the avg):

| Model | Download | Eval | Avg decision | Notes |
|---|---|---|---|---|
| `qwen3.5:2b` (default) | 2.7GB | 18/18 | 1.9s | best accuracy-per-second |
| `qwen3.5:2b-q4_K_M` | 1.9GB | 18/18 | 1.3s | smallest that passes |
| `qwen3.5:4b` | 3.4GB | 18/18 | 2.8s | more headroom on ambiguous cases |
| `qwen3.5:0.8b` | 1.0GB | 15/18 | 0.9s | **not recommended** — its misses were false *allows* (approved writing `curl \| sh` into `~/.zshrc`) |

The failure mode that matters when going smaller is asymmetric: a model
that's wrong by *asking* just costs you a click; a model that's wrong by
*allowing* is a hole. The eval output shows which direction misses go.

## Configuration

Set environment variables in the hook's `command` in `~/.claude/settings.json`,
e.g. `"command": "REVIEWER_MODEL=qwen3.5:9b REVIEWER_ON_DENY=deny python3 ~/.claude/hooks/request-reviewer.py"`.

| Variable | Default | Meaning |
|---|---|---|
| `REVIEWER_MODEL` | `qwen3.5:2b` | Ollama model to use |
| `REVIEWER_ON_DENY` | `ask` | What a model "deny" becomes: `ask` shows you the prompt with the reason; `deny` blocks with no human involved (fully unattended mode) |
| `REVIEWER_TIMEOUT` | `45` | Seconds to wait for the model before falling back to a normal prompt |
| `REVIEWER_OLLAMA_URL` | `http://localhost:11434` | Ollama server |
| `REVIEWER_KEEP_ALIVE` | `30m` | How long Ollama keeps the model resident in RAM |
| `REVIEWER_LOG` | `~/.claude/request-reviewer.log` | JSONL audit log; set to empty to disable |

## Security notes, honestly

A 4B model is not a security boundary. It will catch the obvious stuff
(and the deterministic blocklist catches the worst stuff regardless), but a
sufficiently creative prompt-injected agent could phrase a dangerous action
innocuously. Defaults are chosen accordingly:

- The model can only *approve* on its own; by default a "deny" still shows
  **you** the prompt (`REVIEWER_ON_DENY=ask`).
- The model is instructed to prefer "unsure" over "allow", and hook failures
  always fall back to the normal human prompt.
- The hook is a no-op when you're already running with permissions bypassed.
- Clarifying questions and plan approvals (`AskUserQuestion`, `ExitPlanMode`)
  are never reviewed — those are always yours to answer, even if you widen
  the hook matcher to `*`.
- Everything it decides is in the audit log.

Treat it as a tireless first reviewer that eliminates 90% of the clicking,
not as a sandbox. For truly unattended runs, combine it with an isolated
environment (container/VM).

## License

MIT
