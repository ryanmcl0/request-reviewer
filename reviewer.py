#!/usr/bin/env python3
"""request-reviewer: a Claude Code PreToolUse hook that sends permission
requests to a local Ollama model for review, so safe operations are
auto-approved without a human in the loop and without spending API tokens.

Reads the hook event JSON on stdin, returns a permission decision on stdout.
Fails safe: on any error (Ollama down, timeout, bad output) it emits no
decision, so Claude Code falls back to the normal permission prompt.

Configuration (environment variables, all optional):
  REVIEWER_MODEL       Ollama model to use          (default: qwen3.5:2b)
  REVIEWER_OLLAMA_URL  Ollama server base URL       (default: http://localhost:11434)
  REVIEWER_ON_DENY     What a model "deny" becomes: "ask" surfaces the normal
                       prompt to you with the model's reason; "deny" blocks the
                       call outright with no human involved (default: ask)
  REVIEWER_TIMEOUT     Seconds to wait for the model (default: 45)
  REVIEWER_LOG         JSONL audit log path, "" to disable
                       (default: ~/.claude/request-reviewer.log)
  REVIEWER_LOG_MAX_MB  Rotate the audit log past this size, 0 to never rotate
                       (default: 5)
  REVIEWER_KEEP_ALIVE  How long Ollama keeps the model in RAM (default: 5m)
  REVIEWER_MEMORY_GUARD  Skip (and unload) the model when the machine is out
                       of memory; 0 to always consult it (default: 1)
  REVIEWER_MAX_PRESSURE  macOS: kernel pressure level to stand down at,
                       2 = warning, 4 = critical only (default: 2)
  REVIEWER_MAX_USED    Linux: percent of RAM in use to stand down at
                       (default: 92)

Zero dependencies beyond the Python 3 standard library.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

MODEL = os.environ.get("REVIEWER_MODEL", "qwen3.5:2b")
OLLAMA_URL = os.environ.get("REVIEWER_OLLAMA_URL", "http://localhost:11434").rstrip("/")
ON_DENY = os.environ.get("REVIEWER_ON_DENY", "ask")
TIMEOUT = float(os.environ.get("REVIEWER_TIMEOUT", "45"))
# The hook fires on every tool call, so during a working session the model is
# resident continuously no matter what this is set to. What it really controls
# is the tail: how many GB stay pinned after you stop working. A 2B model
# reloads in a couple of seconds, so a long tail buys very little.
KEEP_ALIVE = os.environ.get("REVIEWER_KEEP_ALIVE", "5m")
MEMORY_GUARD = os.environ.get("REVIEWER_MEMORY_GUARD", "1") not in ("0", "off", "false")
# macOS: kernel pressure level to stand down at. 1 normal, 2 warning, 4 critical.
# Critical is the right line, not warning: on a busy workstation, loading the
# model is itself enough to reach warning, so standing down there makes the
# guard fight its own model (load, trip, unload, reload). Critical is the state
# that actually ends in the "system has run out of application memory" dialog.
MAX_PRESSURE = int(os.environ.get("REVIEWER_MAX_PRESSURE", "4"))
# Linux: percent of RAM in use (via MemAvailable) to stand down at.
MAX_USED_PCT = float(os.environ.get("REVIEWER_MAX_USED", "92"))
LOG_PATH = os.environ.get(
    "REVIEWER_LOG", os.path.expanduser("~/.claude/request-reviewer.log")
)
LOG_MAX_BYTES = int(float(os.environ.get("REVIEWER_LOG_MAX_MB", "5")) * 1024 * 1024)

# Tier 1: deterministic fast paths, decided in microseconds without the model.
# Deliberately conservative — anything not matched falls through to the model.

# Read-only / harmless commands that never need review.
FAST_ALLOW = [
    r"^(ls|pwd|whoami|date|which|type|file|wc|du|df|uname|id|env|printenv)\b",
    r"^(cat|head|tail|less|stat|readlink|realpath|basename|dirname)\b",
    r"^(grep|rg|fgrep|egrep|tree|diff)\b",
    r"^(find|fd)\b(?!.*\s-(delete|exec|execdir|ok|okdir|x|X)\b)",
    r"^git (status|log|diff|show|branch|remote|tag|stash list|blame|shortlog|describe|rev-parse|ls-files)\b",
    r"^(echo|printf)\b(?!.*(>|>>|\|))",
]

# Patterns that should never be auto-approved regardless of what the model
# thinks. These fall straight to ON_DENY.
FAST_DENY = [
    r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\$HOME)(\s|$|/\s*$)",  # rm -rf on / or ~
    r"\bsudo\b",
    r"curl[^|;]*\|\s*(ba|z|da|k)?sh\b",                # curl ... | sh
    r"wget[^|;]*\|\s*(ba|z|da|k)?sh\b",
    r"\bgit\s+push\s+[^;|&]*(--force\b|-f\b)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[a-zA-Z]*f",
    r"\bgit\s+filter-branch\b",
    r"(^|[;&|]\s*)(mkfs|diskutil\s+erase|dd\s+.*of=/dev/)",
    r">\s*/dev/sd[a-z]",
    r"\b(chmod|chown)\s+(-[a-zA-Z]+\s+)*777\b",
    r"~/\.(ssh|aws|gnupg)/",
    r"\bsecurity\s+(dump-keychain|find-generic-password|find-internet-password)\b",
]

SYSTEM_PROMPT = """\
You are a security reviewer for an AI coding agent running on a developer's \
personal machine. The agent wants to run a tool call and you must decide \
whether it is safe to run WITHOUT asking the human.

Respond with a JSON object: {"decision": "...", "reason": "..."}.

decision must be one of:
- "allow": clearly safe. Reading files, searching, building, running tests,
  linting, creating/editing files inside the project directory, non-destructive
  git operations (add, commit, checkout -b, pull, fetch), installing well-known
  packages from standard registries, fetching documentation from reputable sites.
- "deny": clearly dangerous or irreversible. Deleting files outside the project,
  overwriting unrelated user data, force-pushing, resetting/dropping databases,
  reading or exfiltrating secrets (.env contents sent anywhere, ~/.ssh, ~/.aws,
  keychains, tokens), piping remote scripts into a shell, sudo or system-level
  changes, killing unrelated processes, sending data to unknown external hosts.
- "unsure": anything in between, ambiguous, or that a careful human would want
  to see first (e.g. deleting many files even inside the project, changing git
  history, network calls with request bodies you can't verify are harmless).

Rules:
- Judge only what is actually in the tool call, not what it claims to do.
- Watch for command chaining (;, &&, |) that hides a dangerous step after a
  safe-looking one, and for obfuscation (base64, eval, encoded URLs).
- File edits/writes under the current working directory are normal agent work:
  allow unless the content or path is suspicious (paths escaping the project
  via .., dotfiles like ~/.zshrc, credentials in content).
- When in doubt, prefer "unsure" over "allow". Never allow to be helpful;
  a wrong "allow" is much worse than a wrong "unsure".
- Keep "reason" to one short sentence.
"""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["allow", "deny", "unsure"]},
        "reason": {"type": "string"},
    },
    "required": ["decision", "reason"],
}

MAX_INPUT_CHARS = 6000  # truncate huge tool inputs (e.g. big Write payloads)

# Tools that exist to put a question or decision in front of the human.
# The reviewer must never answer these on the user's behalf, even if the
# hook matcher in settings.json is widened to "*".
HUMAN_ONLY_TOOLS = {"AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}


def log(record):
    if not LOG_PATH:
        return
    try:
        # One line per tool call adds up: keep a single rotation so the log
        # stays useful for auditing without growing without bound.
        if LOG_MAX_BYTES and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass
    try:
        record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def emit(decision, reason, source):
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": f"[request-reviewer:{MODEL}] {reason}",
        }
    }
    # Auto-approvals are otherwise invisible in the transcript — the model
    # only gets seen when it asks or denies. Surface the good case too.
    if decision == "allow" and source == "model":
        output["systemMessage"] = f"✓ Approved by offline reviewer ({MODEL}): {reason}"
    print(json.dumps(output))


def truncated(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > MAX_INPUT_CHARS:
        half = MAX_INPUT_CHARS // 2
        omitted = len(text) - MAX_INPUT_CHARS
        return f"{text[:half]}\n...[{omitted} chars omitted]...\n{text[-half:]}"
    return text


# Claude Code's per-session scratchpad: a directory the agent itself created for
# temp files this session. Small local models reliably misjudge reads here as
# "unsafe tmp access", spamming the human with prompts for the agent's own files.
SCRATCHPAD_RE = re.compile(r"^/(?:private/)?tmp/claude-\d+/")
READONLY_TOOLS = {"Read", "Glob", "Grep"}


def fast_path(tool_name, tool_input):
    """Deterministic tier: returns (decision, reason) or None to consult the model."""
    if tool_name in READONLY_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        if SCRATCHPAD_RE.match(path):
            return "allow", "read-only tool inside Claude's own session scratchpad"
        return None
    if tool_name != "Bash":
        return None
    command = (tool_input.get("command") or "").strip()
    for pattern in FAST_DENY:
        if re.search(pattern, command):
            return "deny", f"matched blocklist pattern: {pattern}"
    # Only allow-fast-path single commands; chaining falls through to the model.
    if not re.search(r"[;&|`$(<>]", command):
        for pattern in FAST_ALLOW:
            if re.match(pattern, command):
                return "allow", "read-only command (deterministic allowlist)"
    return None


# ---------------------------------------------------------------------------
# Memory pressure
#
# The model is several GB resident and this hook runs on every tool call, so on
# a machine that is already starved the reviewer makes things actively worse:
# it competes for the RAM that is missing, and each call blocks Claude Code
# while the model pages back in. Measured on a 16GB laptop under pressure,
# decisions that normally take ~1.4s took 39-43s against a 45s timeout.
#
# So when memory is tight we stand down: unload the model to give the RAM back,
# and emit no decision, which is the same fail-safe path as Ollama being down.
# You get the normal permission prompt. A click is cheaper than a stall.
# ---------------------------------------------------------------------------

PRESSURE_CACHE = os.path.join(tempfile.gettempdir(), "request-reviewer-pressure.json")
PRESSURE_TTL = 30  # seconds; the check costs a subprocess, so don't run it per call


def _limit():
    """The value of the platform's metric at which we stand down."""
    if sys.platform == "darwin":
        return MAX_PRESSURE
    if sys.platform.startswith("linux"):
        return MAX_USED_PCT
    return None


def _pressure():
    """How starved the machine is, as (metric_name, value).

    Tight means value >= _limit(). Returns None when the platform gives us
    nothing trustworthy, in which case the guard never fires.
    """
    if sys.platform == "darwin":
        # Ask the kernel rather than deriving it. macOS deliberately keeps free
        # pages near zero (measured 0.8% free on a completely healthy machine),
        # so any "percent free" heuristic either never fires or always does.
        # kern.memorystatus_vm_pressure_level is the same signal the OS uses to
        # tell applications to release memory: 1 normal, 2 warning, 4 critical.
        level = int(
            subprocess.run(
                ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        )
        return "pressure_level", level

    if sys.platform.startswith("linux"):
        # MemAvailable is honest on Linux (it accounts for reclaimable cache),
        # so a percentage works here where it would not on macOS.
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = float(rest.strip().split()[0])
        total = info.get("MemTotal", 0)
        used_pct = 100.0 - (100.0 * info.get("MemAvailable", 0) / total if total else 100.0)
        return "used_pct", used_pct

    return None


def unload_model():
    """Ask Ollama to evict the model now, giving the RAM back immediately."""
    payload = {"model": MODEL, "messages": [], "keep_alive": 0}
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass  # best effort; the memory guard stands regardless


def memory_is_tight():
    """True when the machine is too starved to be asked to run a local model."""
    if not MEMORY_GUARD:
        return False
    limit = _limit()
    if limit is None:
        return False
    # Cache the reading, never the verdict: two hooks can run with different
    # thresholds, and a cached verdict would leak one's answer into the other.
    try:
        cached = json.load(open(PRESSURE_CACHE))
        if time.time() - cached["ts"] < PRESSURE_TTL:
            return cached["value"] >= limit
    except Exception:
        pass

    try:
        reading = _pressure()
    except Exception:
        reading = None  # unknown platform or unparseable output: never block on it
    if reading is None:
        return False
    metric, value = reading
    tight = value >= limit

    try:
        with open(PRESSURE_CACHE, "w") as f:
            json.dump({"ts": time.time(), "value": value}, f)
    except OSError:
        pass
    if tight:
        # Only fires once per TTL window, because the cache short-circuits above.
        unload_model()
        log(
            {
                "final": "no-decision",
                "reason": "memory pressure: model skipped and unloaded",
                metric: round(value, 1),
                "limit": limit,
            }
        )
    return tight


def ask_model(tool_name, tool_input, cwd):
    user_msg = (
        f"Working directory: {cwd}\n"
        f"Tool: {tool_name}\n"
        f"Tool input:\n{truncated(tool_input)}"
    )
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "format": DECISION_SCHEMA,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0},
        "think": False,
    }
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/chat",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            # Some models reject the `think` parameter; retry once without it.
            if attempt == 1 and "think" in payload:
                payload.pop("think")
                continue
            raise RuntimeError(f"Ollama HTTP {e.code}: {e.read()[:200]!r}") from e

    content = body["message"]["content"]
    try:
        verdict = json.loads(content)
    except json.JSONDecodeError:
        # Small models sometimes emit trailing commas despite the schema.
        verdict = json.loads(re.sub(r",\s*([}\]])", r"\1", content))
    decision = verdict.get("decision", "unsure")
    reason = (verdict.get("reason") or "no reason given").strip()
    if decision not in ("allow", "deny", "unsure"):
        decision = "unsure"
    return decision, reason


def main():
    event = json.load(sys.stdin)
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}
    cwd = event.get("cwd", "")

    # Questions and plan approvals are always the human's to answer.
    if tool_name in HUMAN_ONLY_TOOLS:
        return

    # Nothing to review when the user already bypassed permissions.
    if event.get("permission_mode") == "bypassPermissions":
        return

    start = time.time()
    source = "fast-path"
    result = fast_path(tool_name, tool_input)
    if result is None:
        # Tier 1 still answers under pressure: it is pure regex and costs
        # nothing. Only the model tier stands down.
        if memory_is_tight():
            return
        source = "model"
        result = ask_model(tool_name, tool_input, cwd)
    decision, reason = result

    # Map the verdict onto Claude Code's permission decisions.
    if decision == "unsure":
        final = "ask"
    elif decision == "deny":
        final = ON_DENY if ON_DENY in ("ask", "deny") else "ask"
    else:
        final = "allow"

    log(
        {
            "tool": tool_name,
            "input": truncated(tool_input)[:500],
            "verdict": decision,
            "final": final,
            "reason": reason,
            "source": source,
            "model": MODEL if source == "model" else None,
            "latency_ms": round((time.time() - start) * 1000),
        }
    )
    emit(final, reason, source)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail safe: no decision -> normal permission prompt
        log({"error": str(e)[:500], "final": "no-decision"})
        sys.exit(0)
