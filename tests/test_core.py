import json
from pathlib import Path

import pytest

from audiobook_curator.cli import (
    candidate_evidence,
    chapter_rows_from_payload,
    collision_key,
    library_summary,
    multipart_identity,
    normalized_identity,
    quality_score,
    safe_filename,
    write_json,
    whisper_sampling_fractions,
)


def test_safe_filename_removes_apostrophes_but_not_words():
    assert safe_filename("Madeleine L'Engle - A Wrinkle in Time") == "Madeleine LEngle - A Wrinkle in Time"
    assert safe_filename("Gerald’s Game: A Novel") == "Geralds Game - A Novel"


def test_identity_normalization_tolerates_catalog_punctuation():
    assert normalized_identity("Gerald's Game") == normalized_identity("Geralds Game")
    assert normalized_identity("Nightmares & Dreamscapes") == normalized_identity("Nightmares and Dreamscapes")


def test_quality_prefers_lossless_then_bitrate():
    mp3 = {"codec": "mp3", "bitRate": 320_000, "sampleRate": 48_000, "bytes": 20}
    flac = {"codec": "flac", "bitRate": 200_000, "sampleRate": 44_100, "bytes": 10}
    assert quality_score(flac) > quality_score(mp3)
    assert quality_score({**mp3, "bitRate": 256_000}) < quality_score(mp3)


def test_quality_uses_stream_facts_not_filename_or_container_size():
    shallow = {"codec": "flac", "bitDepth": 16, "bitRate": 900_000, "sampleRate": 44_100, "channels": 2, "bytes": 999}
    deep = {"codec": "flac", "bitDepth": 24, "bitRate": 700_000, "sampleRate": 48_000, "channels": 2, "bytes": 1}
    assert quality_score(deep) > quality_score(shallow)
    assert quality_score({**deep, "bytes": 10_000}) == quality_score(deep)


def test_collision_key_groups_encodings_but_preserves_parts():
    first = {"relativePath": "Book/Gerald's Game.mp3"}
    second = {"relativePath": "Book/Geralds Game.flac"}
    assert collision_key(first) == collision_key(second)
    assert collision_key({"relativePath": "Book/Story Part 1.mp3"}) != collision_key({"relativePath": "Book/Story Part 2.mp3"})


def test_multipart_identity_understands_parts_discs_and_episode_numbers():
    assert multipart_identity(Path("Book Part 2 of 4.mp3")) == {"base": "book", "part": 2, "total": 4}
    assert multipart_identity(Path("Book - CD03.flac")) == {"base": "book", "part": 3, "total": None}
    assert multipart_identity(Path("Book.ep7.m4a")) == {"base": "book", "part": 7, "total": None}


def test_audible_chapter_payload_is_normalized_and_clamped_to_media_duration():
    payload = {"content_metadata": {"chapter_info": {"chapters": [
        {"title": "Opening", "start_offset_ms": 0, "length_ms": 4_000},
        {"title": "Ending", "start_offset_ms": 4_000, "length_ms": 7_000},
    ]}}}
    assert chapter_rows_from_payload(payload, 10.0) == [
        {"title": "Opening", "startSeconds": 0.0, "endSeconds": 4.0},
        {"title": "Ending", "startSeconds": 4.0, "endSeconds": 10.0},
    ]


def test_library_summary_counts_metadata_gaps_without_making_delete_claims():
    files = [
        {"bytes": 10, "extension": ".mp3", "error": None, "missing": {"title": True, "author": False, "album": True, "artwork": True, "chapters": True}},
        {"bytes": 20, "extension": ".m4b", "error": "bad", "missing": {"title": False, "author": True, "album": False, "artwork": False, "chapters": False}},
    ]
    assert library_summary(files) == {
        "files": 2, "bytes": 30, "probeFailures": 1, "missingTitle": 1,
        "missingAuthor": 1, "missingAlbum": 1, "missingArtwork": 1, "missingChapters": 1,
    }


def test_audible_candidate_evidence_records_language_narrator_and_edition_type():
    evidence = candidate_evidence("The Book", "The Author", "The Narrator", 3_600, {
        "title": "The Book", "authors": [{"name": "The Author"}],
        "narrators": [{"name": "The Narrator"}], "runtime_length_min": 60,
        "language": "english", "format_type": "unabridged",
    })
    assert evidence["languageMatch"] is True
    assert evidence["narratorMatch"] is True
    assert evidence["unabridged"] is True
    assert evidence["strictIdentityMatch"] is True


def test_select_writes_natural_sequence_and_flags_duration_mismatch(tmp_path: Path):
    from audiobook_curator.cli import main

    inventory = tmp_path / "inventory.json"
    report = tmp_path / "selection.json"
    rows = [
        {"relativePath": name, "path": f"/{name}", "codec": "mp3", "bitRate": 64_000, "sampleRate": 44_100, "durationSeconds": duration}
        for name, duration in [("10.mp3", 10), ("2.mp3", 10), ("1.mp3", 10), ("Same.flac", 10), ("Same.mp3", 20)]
    ]
    inventory.write_text(json.dumps({"files": rows}))
    assert main(["select", "--inventory", str(inventory), "--report", str(report)]) == 0
    selections = json.loads(report.read_text())["selections"]
    assert [row["identityKey"] for row in selections[:3]] == ["1", "2", "10"]
    assert selections[-1]["reviewRequired"] is True


def test_whisper_sampling_adds_new_distributed_windows_without_duplicates():
    assert whisper_sampling_fractions(5) == [0.05, 0.275, 0.5, 0.725, 0.95]
    assert whisper_sampling_fractions(9) == [0.05, 0.275, 0.5, 0.725, 0.95, 0.15, 0.85, 0.375, 0.625]


def test_audible_select_records_explicit_human_choice(tmp_path: Path):
    from audiobook_curator.cli import main

    candidates = tmp_path / "candidates.json"
    receipt = tmp_path / "selected.json"
    candidates.write_text(json.dumps({"candidates": [{"asin": "FIRST"}, {"asin": "SECOND", "evidence": {"strictIdentityMatch": True}}]}))
    assert main(["audible-select", "--candidates", str(candidates), "--candidate", "2", "--receipt", str(receipt)]) == 0
    selected = json.loads(receipt.read_text())
    assert selected["selected"]["asin"] == "SECOND"
    assert selected["humanReviewed"] is True


def test_json_receipt_can_never_target_an_audio_filename(tmp_path: Path):
    with pytest.raises(Exception, match="refusing to write JSON over an audio path"):
        write_json(tmp_path / "book.m4b", {"status": "bad target"})
