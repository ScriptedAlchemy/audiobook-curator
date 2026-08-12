import json
import shutil
import subprocess
from pathlib import Path

import pytest

from audiobook_curator.cli import main


pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")


def tone(path: Path, frequency: int) -> None:
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=0.8",
        "-c:a", "flac", str(path),
    ], check=True)


def test_inventory_select_convert_and_audit(tmp_path: Path):
    source = tmp_path / "A Book"
    source.mkdir()
    tone(source / "01 - Opening.flac", 440)
    tone(source / "02 - Ending.flac", 660)
    inventory = tmp_path / "inventory.json"
    selection = tmp_path / "selection.json"
    plan = tmp_path / "plan.json"
    receipt = tmp_path / "convert.json"
    output = tmp_path / "Geralds Game.m4b"
    audit = tmp_path / "audit.json"

    assert main(["inventory", str(source), "--report", str(inventory)]) == 0
    assert main(["select", "--inventory", str(inventory), "--report", str(selection)]) == 0
    assert main(["convert", "--selection", str(selection), "--output", str(output), "--title", "Gerald's Game", "--author", "Example Author", "--receipt", str(plan)]) == 0
    assert not output.exists()
    assert json.loads(plan.read_text())["status"] == "planned"

    assert main(["convert", "--selection", str(selection), "--output", str(output), "--title", "Gerald's Game", "--author", "Example Author", "--receipt", str(receipt), "--apply"]) == 0
    assert output.is_file()
    assert all(path.is_file() for path in source.iterdir())
    assert main(["audit", "--file", str(output), "--receipt", str(audit), "--full-decode"]) == 0
    result = json.loads(audit.read_text())
    assert result["status"] == "verified"
    assert result["probe"]["chapters"] == 2
    assert len(result["sha256"]) == 64
