#!/usr/bin/env python3
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    codex = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
    claude = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    claude_marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    codex_marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    project = (ROOT / "pyproject.toml").read_text()
    assert codex["name"] == claude["name"] == ROOT.name
    assert re.fullmatch(r"\d+\.\d+\.\d+", codex["version"])
    assert codex["version"] == claude["version"]
    assert claude_marketplace["plugins"][0]["version"] == codex["version"]
    assert f'version = "{codex["version"]}"' in project
    assert f'__version__ = "{codex["version"]}"' in (ROOT / "src/audiobook_curator/__init__.py").read_text()
    assert codex_marketplace["plugins"][0]["name"] == codex["name"]
    assert (ROOT / ".agents/plugins" / codex_marketplace["plugins"][0]["source"]["path"]).resolve() == ROOT
    assert codex["license"] == claude["license"] == "MIT"
    skill = (ROOT / "skills/curate-audiobooks/SKILL.md").read_text()
    assert skill.startswith("---\nname: curate-audiobooks\ndescription:")
    assert "TODO" not in skill
    forbidden = ["/" + "Volumes" + "/", "/" + "Users" + "/", "Pl" + "ex", "scro" + "bbl", "to" + "ken"]
    ignored_parts = {".git", ".venv", ".pytest_cache", "__pycache__", "audiobook_curator.egg-info"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or ignored_parts.intersection(path.parts):
            continue
        if path.suffix.lower() not in {".py", ".md", ".sh", ".toml", ".yaml", ".yml", ".json"}:
            continue
        text = path.read_text(errors="ignore")
        for needle in forbidden:
            assert needle.lower() not in text.lower(), f"forbidden machine/personal term {needle!r} in {path.relative_to(ROOT)}"
    print("repository manifests and privacy fences validated")


if __name__ == "__main__":
    main()
