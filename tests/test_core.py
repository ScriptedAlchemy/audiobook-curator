import json
from pathlib import Path

from audiobook_curator.cli import collision_key, normalized_identity, quality_score, safe_filename


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


def test_collision_key_groups_encodings_but_preserves_parts():
    first = {"relativePath": "Book/Gerald's Game.mp3"}
    second = {"relativePath": "Book/Geralds Game.flac"}
    assert collision_key(first) == collision_key(second)
    assert collision_key({"relativePath": "Book/Story Part 1.mp3"}) != collision_key({"relativePath": "Book/Story Part 2.mp3"})
