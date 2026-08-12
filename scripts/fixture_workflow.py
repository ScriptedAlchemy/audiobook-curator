#!/usr/bin/env python3
"""Run a disposable representative workflow without touching user media."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audiobook_curator.cli import main


def run_fixture() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("ffmpeg and ffprobe are required")
    with tempfile.TemporaryDirectory(prefix="audiobook-curator-fixture-") as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        for name, frequency in [("01 - Opening.flac", 440), ("02 - Ending.flac", 660)]:
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=1", "-c:a", "flac", str(source / name)], check=True)
        inventory, selection = root / "inventory.json", root / "selection.json"
        output, conversion, audit = root / "Example Book.m4b", root / "conversion.json", root / "audit.json"
        product, metadata = root / "product.json", root / "metadata.json"
        product.write_text(json.dumps({"asin": "EXAMPLE123", "title": "Example's Book", "authors": [{"name": "Example Author"}], "narrators": [{"name": "Example Narrator"}], "publisher_name": "Example Publisher", "publisher_summary": "<p>A fixture description.</p>"}))
        commands = [
            ["inventory", str(source), "--report", str(inventory)],
            ["select", "--inventory", str(inventory), "--report", str(selection)],
            ["convert", "--selection", str(selection), "--output", str(output), "--title", "Example's Book", "--author", "Example Author", "--receipt", str(conversion), "--apply"],
            ["apply-metadata", "--file", str(output), "--product", str(product), "--receipt", str(metadata), "--apply"],
            ["audit", "--file", str(output), "--receipt", str(audit), "--full-decode"],
        ]
        for command in commands:
            code = main(command)
            if code:
                if audit.exists():
                    print(audit.read_text())
                raise SystemExit(f"fixture command failed ({code}): {command}")
        result = json.loads(audit.read_text())
        assert result["status"] == "verified"
        assert result["probe"]["chapters"] == 2
        assert len(list(source.iterdir())) == 2
        print(json.dumps({"status": "verified", "sourceFilesPreserved": 2, "outputBytes": output.stat().st_size, "sha256": result["sha256"], "chapters": result["probe"]["chapters"]}, indent=2))


if __name__ == "__main__":
    run_fixture()
