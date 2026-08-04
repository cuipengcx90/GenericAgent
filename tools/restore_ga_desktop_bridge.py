#!/usr/bin/env python3
"""Repair the external GenericAgent core override if an upstream desktop update overwrites settings.
Invoked on demand by launchd WatchPaths; it does not loop or stay resident.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SETTINGS = Path.home() / ".ga_desktop_settings.json"
SOURCE = Path("/Users/cuip/projects/GenericAgent")
APP = "/Applications/GenericAgent.app"
LOG = Path.home() / "Library/Logs/com.genericagent.desktop-bridge-repair.log"
LOCK = Path("/tmp/com.genericagent.desktop-bridge-repair.lock")


def log(message: str) -> None:
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}\n"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fp:
        fp.write(line)


def desktop_is_running() -> bool:
    p = subprocess.run(
        ["pgrep", "-f", "/Applications/GenericAgent.app/Contents/MacOS/ga-desktop"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return p.returncode == 0


def main() -> int:
    if not SOURCE.joinpath("agentmain.py").is_file():
        log(f"ERROR source validation failed: {SOURCE}/agentmain.py missing")
        return 2
    try:
        LOCK.mkdir()
    except FileExistsError:
        log("SKIP another repair instance holds lock")
        return 0
    try:
        if not SETTINGS.is_file():
            log("SKIP settings file absent")
            return 0
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"ERROR cannot read settings: {exc}")
            return 3
        if settings.get("ga_source_override") == str(SOURCE):
            log("SKIP override already correct")
            return 0

        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        backup = SETTINGS.with_name(f"{SETTINGS.name}.pre-bridge-repair.{stamp}")
        shutil.copy2(SETTINGS, backup)
        settings["ga_source_override"] = str(SOURCE)
        tmp = SETTINGS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, SETTINGS)
        log(f"REPAIRED override; backup={backup}")

        # A bridge already started under the overwritten settings has the wrong GA_ROOT.
        # Only restart the UI when it is actually open; launchd's self-trigger from our write exits above.
        if desktop_is_running():
            subprocess.run(["osascript", "-e", 'tell application "GenericAgent" to quit'], check=False)
            time.sleep(2)
            subprocess.run(["open", "-a", APP], check=False)
            log("RESTARTED GenericAgent after repair")
        else:
            log("REPAIRED only; GenericAgent was not running")
        return 0
    finally:
        LOCK.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
