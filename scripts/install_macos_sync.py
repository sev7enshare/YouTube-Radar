"""Render a LaunchAgent for this checkout. Run locally after configuring local.env."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent
    if not (project_dir / "local.env").is_file():
        raise SystemExit("Create local.env from local.env.example first")
    template = (script_dir / "com.youtube-radar.sync.plist.in").read_text()
    output = template.replace("__RADAR_SYNC_SCRIPT__", escape(str(script_dir / "run_sync.sh")))
    target = Path.home() / "Library/LaunchAgents/com.youtube-radar.sync.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise SystemExit(f"Existing LaunchAgent found: {target}. Review it before replacing.")
    target.write_text(output)
    print(f"Created {target}")
    print(f'Run: launchctl bootstrap "gui/$(id -u)" "{target}"')


if __name__ == "__main__":
    main()
