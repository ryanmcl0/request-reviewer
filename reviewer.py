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
  REVIEWER_KEEP_ALIVE  How long Ollama keeps the model in RAM (default: 30m)

Zero dependencies beyond the Python 3 standard library.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

MODEL = os.environ.get("REVIEWER_MODEL", "qwen3.5:2b")
OLLAMA_URL = os.environ.get("REVIEWER_OLLAMA_URL", "http://localhost:11434").rstrip("/")
ON_DENY = os.environ.get("REVIEWER_ON_DENY", "ask")
TIMEOUT = float(os.environ.get("REVIEWER_TIMEOUT", "45"))
KEEP_ALIVE = os.environ.get("REVIEWER_KEEP_ALIVE", "30m")
LOG_PATH = os.environ.get(
    "REVIEWER_LOG", os.path.expanduser("~/.claude/request-reviewer.log")
)

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
        record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def emit(decision, reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": f"[request-reviewer:{MODEL}] {reason}",
                }
            }
        )
    )


def truncated(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > MAX_INPUT_CHARS:
        half = MAX_INPUT_CHARS // 2
        omitted = len(text) - MAX_INPUT_CHARS
        return f"{text[:half]}\n...[{omitted} chars omitted]...\n{text[-half:]}"
    return text


def fast_path(tool_name, tool_input):
    """Deterministic tier: returns (decision, reason) or None to consult the model."""
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
    emit(final, reason)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail safe: no decision -> normal permission prompt
        log({"error": str(e)[:500], "final": "no-decision"})
        sys.exit(0)
