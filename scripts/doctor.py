#!/usr/bin/env python3
"""Read-only environment diagnostic for X Bookmarks + Likes Digest."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import x_bookmark_digest as digest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local dependencies without changing X or local state")
    parser.add_argument("--skip-auth", action="store_true", help="skip the read-only whoami check")
    args = parser.parse_args()
    checks: dict[str, object] = {"python": sys.version.split()[0]}
    try:
        config = digest.load_config()
        checks["config"] = "ok"
        categories = config.get("categories", [])
        names = [item.get("name") for item in categories if isinstance(item, dict) and item.get("name")]
        checks["categories"] = "ok" if names and len(names) == len(set(names)) else "invalid"
        schedule = config.get("defaults", {}).get("schedule", {})
        schedule_time = schedule.get("time") if isinstance(schedule, dict) else None
        if schedule_time in (None, ""):
            checks["schedule"] = "not configured (ask before automation)"
        elif isinstance(schedule_time, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule_time):
            checks["schedule"] = schedule_time
        else:
            checks["schedule"] = "invalid"
    except digest.DigestError as exc:
        checks["config"] = {"ok": False, "error": str(exc)}
    path = shutil.which("opencli")
    checks["opencli"] = path or "missing"
    if path:
        help_proc = digest.subprocess.run([path, "twitter", "--help"], capture_output=True, text=True)
        help_text = f"{help_proc.stdout}\n{help_proc.stderr}"
        checks["twitter_read_commands"] = all(name in help_text for name in ("bookmarks", "likes"))
        if not args.skip_auth:
            try:
                checks["account"] = digest.preflight()
            except digest.DigestError as exc:
                checks["account"] = {"ok": False, "error": str(exc), "kind": exc.kind}
        else:
            checks["account"] = "skipped"
    checks["database_default"] = str(digest.default_db())
    checks["obsidian"] = "configured" if digest.defaults().get("obsidian_vault") else "pending-import fallback"
    checks["session_lock"] = "fcntl" if digest.fcntl is not None else "best-effort (fcntl unavailable)"
    failures = [key for key, value in checks.items() if isinstance(value, dict) and value.get("ok") is False]
    if checks.get("opencli") == "missing":
        failures.append("opencli")
    if checks.get("twitter_read_commands") is False:
        failures.append("twitter_read_commands")
    if checks.get("categories") == "invalid":
        failures.append("categories")
    if checks.get("schedule") == "invalid":
        failures.append("schedule")
    print(json.dumps({"ok": not failures, "checks": checks}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
