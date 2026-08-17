import json
import shutil
import subprocess
from pathlib import Path

import pytest

from audiobook_curator.cli import audio_sha256, main


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
    assert main(["audit", "--file", str(output), "--conversion-receipt", str(receipt), "--receipt", str(audit), "--full-decode"]) == 0
    result = json.loads(audit.read_text())
    assert result["status"] == "verified"
    assert result["probe"]["chapters"] == 2
    assert result["sourceChapterMapping"]["status"] == "verified"
    assert len(result["sha256"]) == 64


def test_library_audit_and_chapter_repair_preserve_audio(tmp_path: Path):
    source = tmp_path / "library" / "Example Author" / "Example Book"
    source.mkdir(parents=True)
    media = source / "Audiobook.m4b"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:a", "aac", "-metadata", "title=Example Book", "-metadata", "artist=Example Author", str(media),
    ], check=True)
    other = tmp_path / "library" / "Other Author" / "Other Book"
    other.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=550:duration=1", "-c:a", "aac", str(other / "Audiobook.m4b")], check=True)
    report = tmp_path / "library-audit.json"
    chapter_spec = tmp_path / "chapters.json"
    plan = tmp_path / "chapter-plan.json"
    receipt = tmp_path / "chapter-receipt.json"

    assert main(["library-audit", str(tmp_path / "library"), "--report", str(report)]) == 0
    audited = json.loads(report.read_text())
    assert audited["summary"]["files"] == 2
    assert audited["summary"]["missingArtwork"] == 2
    assert audited["summary"]["missingChapters"] == 0  # short clips are not presumed broken
    assert audited["duplicateCandidates"] == []

    chapter_spec.write_text(json.dumps({"chapters": [{"title": "Example Book", "startSeconds": 0, "endSeconds": 2}]}))
    before_audio = audio_sha256(media)
    assert main(["apply-chapters", "--file", str(media), "--chapters", str(chapter_spec), "--receipt", str(plan)]) == 0
    assert json.loads(plan.read_text())["status"] == "planned"
    assert main(["apply-chapters", "--file", str(media), "--chapters", str(chapter_spec), "--receipt", str(receipt), "--apply"]) == 0
    applied = json.loads(receipt.read_text())
    assert applied["status"] == "applied-verified"
    assert applied["audioSha256After"] == before_audio
    assert applied["chapterCountAfter"] == 1
    assert applied["verifiedBoundaries"] is True


def test_metadata_update_verifies_tags_and_preserves_existing_cover(tmp_path: Path):
    media = tmp_path / "book.m4b"
    cover = tmp_path / "cover.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x32", "-frames:v", "1", str(cover)], check=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-f", "lavfi", "-i", "sine=frequency=660:duration=1", "-i", str(cover),
        "-map", "0:a:0", "-map", "1:a:0", "-map", "2:v:0", "-c:a", "aac", "-metadata:s:a:0", "language=fra", "-metadata:s:a:1", "language=eng", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic", str(media),
    ], check=True)
    product = tmp_path / "product.json"
    product.write_text(json.dumps({
        "asin": "EXAMPLE", "title": "Correct Title", "authors": [{"name": "Correct Author"}],
        "narrators": [{"name": "Correct Narrator"}], "publisher_name": "Publisher",
        "publisher_summary": "<p>A useful summary.</p>",
    }))
    receipt = tmp_path / "metadata.json"
    before_audio = audio_sha256(media)
    assert main(["apply-metadata", "--file", str(media), "--product", str(product), "--receipt", str(receipt), "--apply"]) == 0
    result = json.loads(receipt.read_text())
    assert result["status"] == "applied-verified"
    assert result["artworkStreamsAfter"] == 1
    assert result["audioSha256After"] == before_audio
    assert result["streamCountAfter"] == 3
    details = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(media)], check=True, capture_output=True, text=True).stdout)
    assert [stream.get("tags", {}).get("language") for stream in details["streams"] if stream["codec_type"] == "audio"] == ["fra", "eng"]


def test_single_m4b_conversion_is_audio_preserving_stream_copy(tmp_path: Path):
    source = tmp_path / "source.m4b"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", str(source)], check=True)
    inventory = tmp_path / "inventory.json"
    selection = tmp_path / "selection.json"
    output = tmp_path / "derived.m4b"
    receipt = tmp_path / "convert.json"
    assert main(["inventory", str(source), "--report", str(inventory)]) == 0
    assert main(["select", "--inventory", str(inventory), "--report", str(selection)]) == 0
    before_audio = audio_sha256(source)
    assert main(["convert", "--selection", str(selection), "--output", str(output), "--title", "Book", "--author", "Author", "--receipt", str(receipt), "--apply"]) == 0
    result = json.loads(receipt.read_text())
    assert result["audioMode"] == "stream-copy"
    assert result["audioSha256"] == before_audio
    assert audio_sha256(output) == before_audio


def test_multipart_conversion_refuses_to_replace_any_selected_source(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    first, second = source / "1.m4b", source / "2.m4b"
    for path, frequency in [(first, 440), (second, 660)]:
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=0.8", "-c:a", "aac", str(path)], check=True)
    inventory = tmp_path / "inventory.json"
    selection = tmp_path / "selection.json"
    receipt = tmp_path / "receipt.json"
    assert main(["inventory", str(source), "--report", str(inventory)]) == 0
    assert main(["select", "--inventory", str(inventory), "--report", str(selection)]) == 0
    before = first.read_bytes()
    assert main(["convert", "--selection", str(selection), "--output", str(first), "--title", "Book", "--author", "Author", "--receipt", str(receipt), "--apply", "--overwrite"]) == 1
    assert first.read_bytes() == before


@pytest.mark.skipif(not shutil.which("audiobook-forge"), reason="Audiobook Forge required")
def test_audiobook_forge_engine_produces_verified_natural_chapters(tmp_path: Path):
    source = tmp_path / "Book"
    source.mkdir()
    tone(source / "1 - Opening.flac", 440)
    tone(source / "2 - Middle.flac", 550)
    tone(source / "10 - Ending.flac", 660)
    inventory = tmp_path / "inventory.json"
    selection = tmp_path / "selection.json"
    output = tmp_path / "Book.m4b"
    receipt = tmp_path / "forge.json"
    audit = tmp_path / "audit.json"
    assert main(["inventory", str(source), "--report", str(inventory)]) == 0
    assert main(["select", "--inventory", str(inventory), "--report", str(selection)]) == 0
    assert main(["convert", "--selection", str(selection), "--output", str(output), "--title", "Book", "--author", "Author", "--engine", "audiobook-forge", "--receipt", str(receipt), "--apply"]) == 0
    result = json.loads(receipt.read_text())
    assert result["engine"] == "audiobook-forge"
    assert result["probe"]["chapters"] == 3
    assert main(["audit", "--file", str(output), "--conversion-receipt", str(receipt), "--receipt", str(audit)]) == 0
