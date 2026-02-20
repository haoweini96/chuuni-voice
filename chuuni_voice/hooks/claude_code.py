"""Claude Code hooks configuration — generate, inject, and remove.

Hook → ChuuniEvent mapping
──────────────────────────────────────────────────────────────────────────────
UserPromptSubmit + (any)                 → THINKING   (fires the instant the user sends a message)
SessionStart     + (any)                 → TASK_START (fires when a session begins or resumes)

PreToolUse  + Write | Edit | MultiEdit   → CODING
PreToolUse  + Bash                       → BASH_RUN

PostToolUse + Bash                       → ERROR      (exit≠0, crash keywords in output)
                                         → TEST_FAIL  (exit≠0, no crash keywords)
                                         → TEST_PASS  (exit=0)
                                           Crash keywords: Traceback, ModuleNotFoundError,
                                           ImportError, SyntaxError, NameError
PostToolUseFailure + (any)               → ERROR      (tool execution failure)

PermissionRequest  + (any)               → PERMISSION_PROMPT (precise: only fires on permission dialogs)
Stop               + (any)               → TASK_DONE
──────────────────────────────────────────────────────────────────────────────
"""

import json
import shutil
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Core: generate hooks config dict
# ---------------------------------------------------------------------------


def generate_hooks_config(chuuni_bin: str | None = None) -> dict:
    """Return a Claude Code hooks config dict.

    The dict maps each hook event name to a list of hook-matcher entries,
    as expected by the ``hooks`` key in ``~/.claude/settings.json``.
    """
    bin_path = chuuni_bin or _find_chuuni_bin()

    def _play(event_value: str) -> dict:
        return {"type": "command", "command": f"{bin_path} play {event_value}"}

    def _on_hook(ctx: str) -> dict:
        return {"type": "command", "command": f"{bin_path} on-hook {ctx}"}

    def _entry(matcher: str, hook: dict) -> dict:
        return {"matcher": matcher, "hooks": [hook]}

    return {
        "UserPromptSubmit": [
            _entry("", _play("thinking")),
        ],
        "SessionStart": [
            _entry("", {"type": "command", "command": f"{bin_path} _session-start"}),
        ],
        "PreToolUse": [
            _entry("Write|Edit|MultiEdit", _play("coding")),
            _entry("Bash",                 _play("bash_run")),
        ],
        "PostToolUse": [
            _entry("Bash", _on_hook("post-bash")),
        ],
        "PostToolUseFailure": [
            _entry("", _play("error")),
        ],
        "PermissionRequest": [
            _entry("", _play("permission_prompt")),
        ],
        "Stop": [
            _entry("", _play("task_done")),
        ],
    }


# ---------------------------------------------------------------------------
# inject_hooks
# ---------------------------------------------------------------------------


def inject_hooks(settings_path: Path | None = None) -> None:
    """Inject chuuni hooks into Claude Code settings.json.

    - Backs up the original file to settings.json.bak before any change.
    - Creates the file if it does not exist.
    - Merges with existing hooks: user's own entries are preserved.
    - Idempotent: removes stale chuuni entries before re-injecting so
      running this twice never duplicates hooks.
    - Prints a summary of what was injected.
    """
    target = Path(settings_path) if settings_path else _default_settings_path()
    is_new = not target.exists()

    # Load existing settings
    settings: dict = {}
    if not is_new:
        try:
            with target.open() as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            settings = {}
        # Backup before any mutation
        backup = target.with_suffix(".json.bak")
        shutil.copy2(target, backup)
        print(f"  Backed up  →  {backup}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"  Creating   →  {target}")

    # Remove stale chuuni entries (idempotent re-injection)
    existing_hooks: dict[str, list] = settings.get("hooks", {})
    for event in list(existing_hooks):
        existing_hooks[event] = [
            e for e in existing_hooks[event]
            if not _is_chuuni_entry(e)
        ]

    # Inject fresh entries
    chuuni_bin = _find_chuuni_bin()
    new_hooks = generate_hooks_config(chuuni_bin)
    for event, entries in new_hooks.items():
        existing_hooks.setdefault(event, [])
        existing_hooks[event].extend(entries)

    # Drop now-empty event lists
    settings["hooks"] = {k: v for k, v in existing_hooks.items() if v}

    with target.open("w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    # Summary
    total = sum(len(v) for v in new_hooks.items())
    total_entries = sum(len(v) for v in new_hooks.values())
    print(f"  Injected   →  {target}")
    print(f"  {total_entries} entries across {len(new_hooks)} events:\n")
    for event, entries in new_hooks.items():
        for entry in entries:
            matcher = entry.get("matcher") or "(catch-all)"
            cmd = entry["hooks"][0]["command"]
            print(f"    {event:<14}  [{matcher:<20}]  →  {cmd}")


# ---------------------------------------------------------------------------
# remove_hooks
# ---------------------------------------------------------------------------


def remove_hooks(settings_path: Path | None = None) -> None:
    """Remove all chuuni-injected hooks from Claude Code settings.json.

    Identified by the binary name: any hook entry whose every command
    begins with a binary named ``chuuni`` is considered chuuni-owned.
    User's own hooks are left untouched.
    Backs up the file before modifying.
    """
    target = Path(settings_path) if settings_path else _default_settings_path()

    if not target.exists():
        print(f"  {target} does not exist — nothing to do.")
        return

    try:
        with target.open() as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  Could not read {target}: {exc}")
        return

    existing_hooks: dict[str, list] = settings.get("hooks", {})
    removed = 0

    for event in list(existing_hooks):
        before = len(existing_hooks[event])
        existing_hooks[event] = [
            e for e in existing_hooks[event]
            if not _is_chuuni_entry(e)
        ]
        removed += before - len(existing_hooks[event])

    # Drop now-empty event lists; drop hooks key if nothing remains
    clean = {k: v for k, v in existing_hooks.items() if v}
    if clean:
        settings["hooks"] = clean
    elif "hooks" in settings:
        del settings["hooks"]

    # Backup then write
    backup = target.with_suffix(".json.bak")
    shutil.copy2(target, backup)
    print(f"  Backed up  →  {backup}")

    with target.open("w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    if removed:
        print(f"  Removed    →  {removed} chuuni hook entries from {target}")
    else:
        print(f"  No chuuni hooks found in {target} — nothing removed.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _is_chuuni_entry(entry: dict) -> bool:
    """Return True if every hook command in this entry is a chuuni command.

    Detection: the base name of the first word in the command is ``chuuni``.
    This is robust to full paths (e.g. /home/user/.venv/bin/chuuni play ...).
    """
    hooks = entry.get("hooks", [])
    if not hooks:
        return False
    return all(
        Path(h.get("command", "").split()[0]).name == "chuuni"
        for h in hooks
        if h.get("command")
    )


def _find_chuuni_bin() -> str:
    """Return the absolute path to the chuuni binary.

    Search order:
      1. shutil.which("chuuni")          — works when chuuni is on PATH
      2. Path(sys.executable).parent / "chuuni"  — same venv as running Python
      3. bare "chuuni"                   — last resort (likely to fail in hooks)
    """
    found = shutil.which("chuuni")
    if found:
        return found

    # Claude Code hooks run in a non-interactive shell that may not have the
    # venv on PATH.  Derive the path from the current Python interpreter.
    candidate = Path(sys.executable).parent / "chuuni"
    if candidate.exists():
        return str(candidate)

    return "chuuni"
