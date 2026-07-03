#!/usr/bin/env bash
# Installer for request-reviewer, a local-LLM permission reviewer for Claude Code.
#
# Usage:
#   ./install.sh                # install with the default model (qwen3.5:4b)
#   ./install.sh qwen3.5:9b     # install with a different Ollama model
#   ./install.sh --uninstall    # remove the hook and settings entry
set -euo pipefail

HOOK_DIR="$HOME/.claude/hooks"
HOOK_PATH="$HOOK_DIR/request-reviewer.py"
SETTINGS="$HOME/.claude/settings.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="${1:-qwen3.5:2b}"
MATCHER="*"

merge_settings() {
  python3 - "$SETTINGS" "$HOOK_PATH" "$MATCHER" "$1" <<'PY'
import json, os, shutil, sys

settings_path, hook_path, matcher, action = sys.argv[1:5]
settings = {}
if os.path.exists(settings_path):
    shutil.copy(settings_path, settings_path + ".bak")
    with open(settings_path) as f:
        settings = json.load(f)

hooks = settings.setdefault("hooks", {})
pre = hooks.setdefault("PreToolUse", [])
# Drop any previous request-reviewer entry (idempotent install / uninstall).
pre[:] = [
    entry for entry in pre
    if not any("request-reviewer" in h.get("command", "") for h in entry.get("hooks", []))
]

if action == "install":
    pre.append({
        "matcher": matcher,
        "hooks": [{"type": "command", "command": f"python3 {hook_path}"}],
    })

if not pre:
    hooks.pop("PreToolUse")
if not hooks:
    settings.pop("hooks", None)

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print(f"{action}ed hook in {settings_path} (backup at {settings_path}.bak)")
PY
}

if [[ "${1:-}" == "--uninstall" ]]; then
  merge_settings uninstall
  rm -f "$HOOK_PATH"
  echo "Removed $HOOK_PATH. The model and log file were left in place:"
  echo "  ollama rm <model>                  # to free disk space"
  echo "  rm ~/.claude/request-reviewer.log  # to remove the audit log"
  exit 0
fi

if ! command -v ollama >/dev/null; then
  echo "Ollama is required but not installed."
  echo "  macOS:  brew install ollama && brew services start ollama"
  echo "  Linux:  curl -fsSL https://ollama.com/install.sh | sh"
  echo "  Or download from https://ollama.com/download"
  exit 1
fi

if ! curl -sf http://localhost:11434/api/version >/dev/null; then
  echo "Ollama is installed but the server isn't running."
  echo "  macOS (brew): brew services start ollama"
  echo "  Otherwise:    ollama serve   (or open the Ollama app)"
  exit 1
fi

echo "Pulling model $MODEL (skips if already present)..."
ollama pull "$MODEL"

mkdir -p "$HOOK_DIR"
cp "$SCRIPT_DIR/reviewer.py" "$HOOK_PATH"
chmod +x "$HOOK_PATH"
merge_settings install

if [[ "$MODEL" != "qwen3.5:2b" ]]; then
  echo
  echo "NOTE: you chose a non-default model. Set it in the hook entry in $SETTINGS:"
  echo "  \"command\": \"REVIEWER_MODEL=$MODEL python3 $HOOK_PATH\""
fi

echo
echo "Smoke test (safe command should be allowed)..."
RESULT=$(echo '{"tool_name":"Bash","tool_input":{"command":"git status"},"cwd":"'"$PWD"'","permission_mode":"default"}' \
  | REVIEWER_MODEL="$MODEL" python3 "$HOOK_PATH")
echo "$RESULT"
if echo "$RESULT" | grep -q '"permissionDecision": *"allow"'; then
  echo "✓ request-reviewer is installed and working."
  echo "  Audit log: ~/.claude/request-reviewer.log"
  echo "  Evaluate model quality with: python3 eval.py"
else
  echo "✗ Smoke test did not return an allow decision — check that Ollama is running."
  exit 1
fi
