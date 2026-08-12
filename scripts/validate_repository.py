#!/usr/bin/env python3
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    codex = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
    claude = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    assert codex["name"] == claude["name"] == ROOT.name
    assert re.fullmatch(r"\d+\.\d+\.\d+", codex["version"])
    assert codex["version"] == claude["version"]
    assert codex["license"] == claude["license"] == "MIT"
    skill = (ROOT / "skills/curate-audiobooks/SKILL.md").read_text()
    assert skill.startswith("---\nname: curate-audiobooks\ndescription:")
    assert "TODO" not in skill
    forbidden = ["/Volumes/", "/Users/", "Plex", "scrobbl", "token"]
    checked = [ROOT / "src", ROOT / "skills", ROOT / "commands", ROOT / "README.md"]
    for target in checked:
        paths = target.rglob("*") if target.is_dir() else [target]
        for path in paths:
            if path.is_file() and path.suffix.lower() in {".py", ".md", ".yaml", ".yml", ".json"}:
                text = path.read_text(errors="ignore")
                for needle in forbidden:
                    assert needle.lower() not in text.lower(), f"forbidden machine/personal term {needle!r} in {path.relative_to(ROOT)}"
    print("repository manifests and privacy fences validated")


if __name__ == "__main__":
    main()
