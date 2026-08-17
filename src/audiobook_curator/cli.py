from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import __version__


AUDIO_EXTENSIONS = {".aac", ".aax", ".aaxc", ".aif", ".aiff", ".flac", ".m4a", ".m4b", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
AUDIBLE_HOSTS = {
    "us": "api.audible.com", "uk": "api.audible.co.uk", "ca": "api.audible.ca",
    "au": "api.audible.com.au", "fr": "api.audible.fr", "de": "api.audible.de",
    "jp": "api.audible.co.jp", "it": "api.audible.it", "in": "api.audible.in",
    "es": "api.audible.es",
}
USER_AGENT = f"audiobook-curator/{__version__} (+https://github.com/ScriptedAlchemy/audiobook-curator)"


class CuratorError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    if path.suffix.casefold() in AUDIO_EXTENSIONS:
        raise CuratorError(f"refusing to write JSON over an audio path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def protect_json_output(path: Path, *protected: Path | None) -> None:
    target = path.resolve()
    for source in protected:
        if source is not None and target == source.resolve():
            raise CuratorError(f"JSON output collides with an input or media target: {target}")


def run(command: list[str], *, timeout: int = 600, capture: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, text=True, capture_output=capture, timeout=timeout)
    except FileNotFoundError as error:
        raise CuratorError(f"required executable not found: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise CuratorError(f"command failed ({command[0]}): {detail}") from error


def ffprobe(path: Path) -> dict[str, Any]:
    completed = run([
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format",
        "-show_streams", "-show_chapters", str(path),
    ], timeout=600)
    return json.loads(completed.stdout)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audio_sha256(path: Path, index: int = 0) -> str:
    result = run([
        "ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:a:{index}", "-c", "copy",
        "-f", "hash", "-hash", "sha256", "-",
    ], timeout=7200)
    return result.stdout.strip().removeprefix("SHA256=")


def audio_hashes(path: Path, details: dict[str, Any] | None = None) -> list[str]:
    details = details or ffprobe(path)
    count = sum(1 for stream in details.get("streams", []) if stream.get("codec_type") == "audio")
    return [audio_sha256(path, index) for index in range(count)]


def stream_signature(details: dict[str, Any], *, include_artwork: bool = True) -> list[dict[str, Any]]:
    rows = []
    for stream in details.get("streams", []):
        artwork = bool(stream.get("disposition", {}).get("attached_pic"))
        chapter_track = stream.get("codec_type") == "data" and stream.get("codec_tag_string") == "text"
        if chapter_track:
            continue
        if artwork and not include_artwork:
            continue
        rows.append({
            "codecType": stream.get("codec_type"), "codec": stream.get("codec_name"),
            "sampleRate": stream.get("sample_rate"), "channels": stream.get("channels"),
            "width": stream.get("width"), "height": stream.get("height"), "attachedPicture": artwork,
        })
    return rows


def stable_format_tags(details: dict[str, Any]) -> dict[str, str]:
    return {
        str(key).casefold(): str(value)
        for key, value in details.get("format", {}).get("tags", {}).items()
        if str(key).casefold() != "encoder"
    }


def ensure_supported_auxiliary_streams(details: dict[str, Any]) -> None:
    unsupported = [
        stream for stream in details.get("streams", [])
        if stream.get("codec_type") == "data" and stream.get("codec_tag_string") != "text"
    ]
    if unsupported:
        raise CuratorError("unsupported non-chapter data stream; refusing a metadata-only replacement")


def media_record(path: Path, root: Path | None = None) -> dict[str, Any]:
    details = ffprobe(path)
    return media_record_from_details(path, details, root)


def media_record_from_details(path: Path, details: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    audio = next((stream for stream in details.get("streams", []) if stream.get("codec_type") == "audio" and not stream.get("disposition", {}).get("attached_pic")), None)
    if not audio:
        raise CuratorError(f"no audio stream: {path}")
    stat = path.stat()
    relative = str(path.relative_to(root)) if root else path.name
    return {
        "path": str(path.resolve()), "relativePath": relative, "bytes": stat.st_size,
        "extension": path.suffix.lower(), "durationSeconds": float(details.get("format", {}).get("duration") or 0),
        "codec": audio.get("codec_name"), "bitRate": int(audio.get("bit_rate") or details.get("format", {}).get("bit_rate") or 0),
        "sampleRate": int(audio.get("sample_rate") or 0), "channels": audio.get("channels"),
        "channelLayout": audio.get("channel_layout"), "sampleFormat": audio.get("sample_fmt"),
        "bitDepth": int(audio.get("bits_per_raw_sample") or audio.get("bits_per_sample") or 0),
        "tags": details.get("format", {}).get("tags", {}), "chapters": len(details.get("chapters", [])),
        "artworkStreams": sum(1 for stream in details.get("streams", []) if stream.get("disposition", {}).get("attached_pic")),
    }


def natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def safe_filename(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("'", "").replace("’", "")
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value or "Untitled Audiobook"


def normalized_identity(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char)).casefold()
    value = re.sub(r"[’']s\b", "s", value).replace("&", " and ")
    return " ".join(word for word in re.sub(r"[^a-z0-9]+", " ", value).split() if word not in {"a", "an", "the"})


def quality_score(row: dict[str, Any]) -> tuple[int, int, int, int]:
    codec = str(row.get("codec") or "").lower()
    lossless = 1 if codec in {"flac", "alac", "pcm_s16le", "pcm_s24le", "wavpack"} else 0
    return (
        lossless,
        int(row.get("bitDepth") or 0) if lossless else 0,
        int(row.get("sampleRate") or 0),
        int(row.get("bitRate") or 0),
    )


def multipart_identity(path: Path) -> dict[str, Any] | None:
    name = path.stem
    patterns = [
        r"^(.*?)[\s._\-(\[]+part\s*(\d+)\s*(?:of|/)[\s]*(\d+)[\])\s._-]*$",
        r"^(.*?)[\s._\-(\[]+(?:disc|disk|cd)\s*(\d+)[\])\s._-]*$",
        r"^(.*?)[\s._-]+ep(?:isode)?\s*(\d+)[\s._-]*$",
    ]
    for pattern in patterns:
        match = re.match(pattern, name, re.I)
        if match:
            return {
                "base": re.sub(r"[\s._-]+$", "", match.group(1)).casefold(),
                "part": int(match.group(2)),
                "total": int(match.group(3)) if match.lastindex and match.lastindex >= 3 and match.group(3) else None,
            }
    return None


def library_summary(files: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "files": len(files),
        "bytes": sum(int(row.get("bytes") or 0) for row in files),
        "probeFailures": sum(bool(row.get("error")) for row in files),
        "missingTitle": sum(bool(row.get("missing", {}).get("title")) for row in files),
        "missingAuthor": sum(bool(row.get("missing", {}).get("author")) for row in files),
        "missingAlbum": sum(bool(row.get("missing", {}).get("album")) for row in files),
        "missingArtwork": sum(bool(row.get("missing", {}).get("artwork")) for row in files),
        "missingChapters": sum(bool(row.get("missing", {}).get("chapters")) for row in files),
    }


def audit_media_record(path: Path, root: Path) -> dict[str, Any]:
    try:
        details = ffprobe(path)
        record = media_record_from_details(path, details, root)
        tags = {str(key).casefold(): value for key, value in details.get("format", {}).get("tags", {}).items()}
        duration = record["durationSeconds"]
        record.update({
            "error": None,
            "missing": {
                "title": not tags.get("title"),
                "author": not (tags.get("artist") or tags.get("album_artist") or tags.get("composer") or tags.get("author")),
                "album": not tags.get("album"),
                "artwork": record["artworkStreams"] == 0,
                "chapters": duration >= 600 and record["chapters"] == 0,
            },
        })
        return record
    except Exception as error:
        stat = path.stat()
        return {
            "path": str(path.resolve()), "relativePath": str(path.relative_to(root)),
            "bytes": stat.st_size, "extension": path.suffix.casefold(), "error": str(error),
            "missing": {"title": False, "author": False, "album": False, "artwork": False, "chapters": False},
        }


def command_library_audit(args: argparse.Namespace) -> int:
    roots = [root.resolve() for root in args.sources]
    protect_json_output(args.report, *(root if root.is_file() else None for root in roots))
    candidates: list[tuple[Path, Path]] = []
    for root in roots:
        if root.is_file():
            if root.suffix.casefold() in AUDIO_EXTENSIONS:
                candidates.append((root.parent, root))
        elif root.is_dir():
            candidates.extend((root, path) for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in AUDIO_EXTENSIONS)
        else:
            raise CuratorError(f"source does not exist: {root}")
    candidates.sort(key=lambda pair: natural_key(str(pair[1])))
    files: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {executor.submit(audit_media_record, path, root): (root, path) for root, path in candidates}
        for future in as_completed(futures):
            files.append(future.result())
    files.sort(key=lambda row: natural_key(row["path"]))
    duplicate_groups: dict[str, list[str]] = {}
    multipart_groups: dict[str, list[dict[str, Any]]] = {}
    for row in files:
        relative = Path(row["relativePath"])
        duplicate_key = f"{normalized_identity(str(relative.parent))}/{normalized_identity(relative.stem)}"
        duplicate_groups.setdefault(duplicate_key, []).append(row["path"])
        identity = multipart_identity(Path(row["path"]))
        if identity:
            key = f"{Path(row['path']).parent}\0{identity['base']}"
            multipart_groups.setdefault(key, []).append({"path": row["path"], "part": identity["part"], "total": identity["total"]})
    report = {
        "schemaVersion": 1, "generatedAt": utc_now(), "operation": "library-audit", "mutation": False,
        "sources": [str(root) for root in roots], "summary": library_summary(files), "files": files,
        "duplicateCandidates": [{"identityKey": key, "files": paths} for key, paths in sorted(duplicate_groups.items()) if len(paths) > 1],
        "multipartCandidates": [
            {"directory": key.split("\0", 1)[0], "identityKey": key.split("\0", 1)[1], "files": sorted(rows, key=lambda row: row["part"])}
            for key, rows in sorted(multipart_groups.items()) if len(rows) > 1
        ],
        "reviewNote": "Duplicate and multipart groups are review candidates, never deletion instructions.",
    }
    write_json(args.report, report)
    print(args.report)
    return 1 if args.strict and report["summary"]["probeFailures"] else 0


def command_inventory(args: argparse.Namespace) -> int:
    source = args.source.resolve()
    protect_json_output(args.report, source if source.is_file() else None)
    if source.is_file():
        paths = [source] if source.suffix.lower() in AUDIO_EXTENSIONS else []
        root = source.parent
    else:
        root = source
        paths = [path for path in source.rglob("*") if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS]
    paths.sort(key=lambda path: natural_key(str(path.relative_to(root))))
    rows, errors = [], []
    for path in paths:
        try:
            rows.append(media_record(path, root))
        except Exception as error:  # retain per-file evidence
            errors.append({"path": str(path), "error": str(error)})
    report = {
        "schemaVersion": 1, "generatedAt": utc_now(), "operation": "inventory", "mutation": False,
        "source": str(source), "summary": {"files": len(rows), "errors": len(errors), "bytes": sum(row["bytes"] for row in rows), "durationSeconds": sum(row["durationSeconds"] for row in rows)},
        "files": rows, "errors": errors,
    }
    write_json(args.report, report)
    print(args.report)
    return 1 if errors and args.strict else 0


def collision_key(row: dict[str, Any]) -> str:
    relative = Path(row["relativePath"])
    # Keep chapter/part numbers: they define sequence and must never collapse.
    # Only alternate encodings of the same relative stem compete for selection.
    return "/".join(normalized_identity(part) for part in (*relative.parts[:-1], relative.stem))


def command_select(args: argparse.Namespace) -> int:
    protect_json_output(args.report, args.inventory)
    inventory = load_json(args.inventory)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in inventory["files"]:
        grouped.setdefault(collision_key(row), []).append(row)
    selections = []
    for key, candidates in sorted(grouped.items(), key=lambda item: natural_key(item[0])):
        ordered = sorted(candidates, key=quality_score, reverse=True)
        durations = [float(row.get("durationSeconds") or 0) for row in ordered]
        duration_spread = max(durations) - min(durations) if durations else 0
        review_required = len(ordered) > 1 and duration_spread > max(1.0, max(durations) * 0.01)
        selections.append({
            "identityKey": key, "selected": ordered[0], "alternates": ordered[1:],
            "reason": "lossless codec, lossless bit depth, sample rate, then bitrate from the probed audio stream",
            "durationSpreadSeconds": duration_spread,
            "reviewRequired": review_required,
            "reviewReason": "same-name candidates differ materially in duration" if review_required else None,
        })
    report = {"schemaVersion": 1, "generatedAt": utc_now(), "operation": "quality-selection", "mutation": False, "inventory": str(args.inventory.resolve()), "selections": selections}
    write_json(args.report, report)
    print(args.report)
    return 0


def chapter_title(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    return " - ".join((*relative.parts[:-1], relative.stem))


def ffmetadata(inputs: list[Path], root: Path, metadata: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return str(value).replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", "\\n")
    lines = [";FFMETADATA1"]
    for key, value in metadata.items():
        if value not in (None, ""):
            lines.append(f"{key}={esc(value)}")
    start_ms = 0
    for path in inputs:
        duration_ms = round(media_record(path)["durationSeconds"] * 1000)
        end_ms = start_ms + max(duration_ms, 1)
        lines.extend(["[CHAPTER]", "TIMEBASE=1/1000", f"START={start_ms}", f"END={end_ms}", f"title={esc(chapter_title(path, root))}"])
        start_ms = end_ms
    return "\n".join(lines) + "\n"


def ffmetadata_with_existing_chapters(details: dict[str, Any], metadata: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return str(value).replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", "\\n")
    lines = [";FFMETADATA1"]
    for key, value in metadata.items():
        if value not in (None, ""):
            lines.append(f"{key}={esc(value)}")
    for chapter in details.get("chapters", []):
        lines.extend([
            "[CHAPTER]", "TIMEBASE=1/1000",
            f"START={round(float(chapter.get('start_time') or 0) * 1000)}",
            f"END={round(float(chapter.get('end_time') or 0) * 1000)}",
            f"title={esc(chapter.get('tags', {}).get('title') or '')}",
        ])
    return "\n".join(lines) + "\n"


def selected_paths(selection_path: Path) -> list[Path]:
    payload = load_json(selection_path)
    return [Path(row["selected"]["path"]) for row in payload["selections"]]


def command_convert(args: argparse.Namespace) -> int:
    inputs = selected_paths(args.selection)
    if not inputs:
        raise CuratorError("selection contains no audio files")
    output = args.output.resolve()
    if output.suffix.lower() != ".m4b":
        output = output / f"{safe_filename(args.title)}.m4b"
    protect_json_output(args.receipt, args.selection, output, *inputs)
    if any(output == path.resolve() for path in inputs):
        raise CuratorError("convert output must differ from the source; use apply-metadata or apply-chapters for an existing derived M4B")
    source_records = [media_record(path) for path in inputs]
    expected_duration = sum(row["durationSeconds"] for row in source_records)
    common_root = Path(os.path.commonpath([str(path.parent) for path in inputs]))
    preserve_single_m4b = len(inputs) == 1 and inputs[0].suffix.casefold() == ".m4b"
    source_details = ffprobe(inputs[0]) if preserve_single_m4b else None
    expected_chapters = len(source_details.get("chapters", [])) if source_details and source_details.get("chapters") else len(inputs)
    if source_details and source_details.get("chapters"):
        expected_chapter_rows, _ = chapter_issues(source_details)
    else:
        expected_chapter_rows = []
        start = 0.0
        for path, record in zip(inputs, source_records, strict=True):
            end = start + record["durationSeconds"]
            expected_chapter_rows.append({"number": len(expected_chapter_rows) + 1, "startSeconds": start, "endSeconds": end, "title": chapter_title(path, common_root)})
            start = end
    plan = {
        "schemaVersion": 1, "generatedAt": utc_now(), "operation": "convert", "apply": args.apply,
        "sourcesPreserved": True, "inputs": [str(path.resolve()) for path in inputs], "output": str(output),
        "embeddedMetadata": {"title": args.title, "album": args.title, "artist": args.author, "album_artist": args.author, "composer": args.narrator, "date": args.year, "language": args.language},
        "filenamePolicy": "apostrophes removed from filesystem name; embedded punctuation retained",
        "expectedDurationSeconds": expected_duration, "expectedChapterCount": expected_chapters,
        "expectedChapters": expected_chapter_rows,
        "engine": "ffmpeg" if preserve_single_m4b else args.engine,
        "audioMode": "stream-copy" if preserve_single_m4b else ("Audiobook Forge source quality" if args.engine == "audiobook-forge" else "AAC transcode"),
    }
    if not args.apply:
        write_json(args.receipt, {**plan, "status": "planned"})
        print(args.receipt)
        return 0
    if output.exists() and not args.overwrite:
        raise CuratorError(f"output exists; refusing to overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".audiobook-curator-", dir=output.parent) as directory:
        work = Path(directory)
        concat = work / "concat.txt"
        concat.write_text("".join(f"file '{str(path.resolve()).replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n" for path in inputs), encoding="utf-8")
        metadata_path = work / "metadata.txt"
        metadata = {"title": args.title, "album": args.title, "artist": args.author, "album_artist": args.author, "composer": args.narrator, "date": args.year, "language": args.language}
        metadata_path.write_text(ffmetadata_with_existing_chapters(source_details, metadata) if source_details and source_details.get("chapters") else ffmetadata(inputs, common_root, metadata), encoding="utf-8")
        temporary = work / "output.m4b"
        if args.engine == "audiobook-forge" and not preserve_single_m4b:
            forge_root = work / "forge-root"
            forge_book = forge_root / safe_filename(args.title)
            forge_output = work / "forge-output"
            forge_book.mkdir(parents=True)
            forge_output.mkdir()
            for index, path in enumerate(inputs, 1):
                staged = forge_book / f"{index:06d} - {chapter_title(path, common_root)}{path.suffix.casefold()}"
                try:
                    os.link(path, staged)
                except OSError:
                    staged.symlink_to(path)
            if args.artwork:
                shutil.copy2(args.artwork.resolve(), forge_book / f"cover{args.artwork.suffix.casefold()}")
            forge_command = [
                args.forge_cli, "build", "--root", str(forge_root), "--out", str(forge_output),
                "--parallel", "1", "--skip-existing", "false", "--quality", "source",
                "--aac-encoder", args.forge_aac_encoder, "--chapter-source", "files",
            ]
            if args.language is not None:
                forge_command.extend(["--language", args.language])
            run(forge_command, timeout=args.timeout_hours * 3600)
            forge_results = list(forge_output.rglob("*.m4b"))
            if len(forge_results) != 1:
                raise CuratorError(f"Audiobook Forge produced {len(forge_results)} M4B files; expected exactly one")
            command = [
                "ffmpeg", "-v", "error", "-xerror", "-i", str(forge_results[0]),
                "-f", "ffmetadata", "-i", str(metadata_path), "-map", "0:a:0", "-map", "0:v?",
                "-map_metadata", "1", "-map_chapters", "1", "-c", "copy", "-disposition:v:0", "attached_pic",
            ]
            if args.language is not None:
                command.extend(["-metadata:s:a:0", f"language={args.language}"])
            command.extend(["-movflags", "+faststart", str(temporary)])
            run(command, timeout=args.timeout_hours * 3600)
        else:
            command = ["ffmpeg", "-v", "error", "-xerror", "-f", "concat", "-safe", "0", "-i", str(concat), "-f", "ffmetadata", "-i", str(metadata_path)]
            if args.artwork:
                command.extend(["-i", str(args.artwork.resolve())])
            command.extend(["-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1"])
            if args.artwork:
                command.extend(["-map", "2:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"])
            elif preserve_single_m4b and any(stream.get("disposition", {}).get("attached_pic") for stream in source_details.get("streams", [])):
                command.extend(["-map", "0:v?", "-c:v", "copy", "-disposition:v:0", "attached_pic"])
            if preserve_single_m4b:
                command.extend(["-c:a", "copy"])
            else:
                command.extend(["-c:a", "aac", "-b:a", args.audio_bitrate])
            if args.language is not None:
                command.extend(["-metadata:s:a:0", f"language={args.language}"])
            command.extend(["-movflags", "+faststart", str(temporary)])
            run(command, timeout=args.timeout_hours * 3600, capture=True)
        before_audio = audio_sha256(temporary)
        result = media_record(temporary)
        duration_delta = abs(result["durationSeconds"] - expected_duration)
        staged_details = ffprobe(temporary)
        staged_chapters, staged_issues = chapter_issues(staged_details)
        mapping_issues = chapter_mapping_issues(expected_chapter_rows, staged_chapters)
        if result["chapters"] != expected_chapters or duration_delta > 2.0 or staged_issues or mapping_issues:
            raise CuratorError("converted output failed chapter-count or duration verification; destination left untouched")
        if preserve_single_m4b and before_audio != audio_sha256(inputs[0]):
            raise CuratorError("single-M4B stream copy changed audio; destination left untouched")
        os.replace(temporary, output)
    receipt = {
        **plan, "status": "converted-verified", "outputBytes": output.stat().st_size,
        "outputSha256": sha256(output), "audioSha256": before_audio, "probe": result,
        "durationDeltaSeconds": duration_delta,
    }
    write_json(args.receipt, receipt)
    print(args.receipt)
    return 0


def audible_url(region: str, route: str, query: dict[str, Any]) -> str:
    return f"https://{AUDIBLE_HOSTS[region]}{route}?{urllib.parse.urlencode(query)}"


def fetch(url: str, *, attempts: int = 4, timeout: int = 30) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as error:
            last = error
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 8))
    raise CuratorError(f"download failed after {attempts} attempts: {url}: {last}")


def candidate_evidence(title: str, author: str | None, narrator: str | None, duration: float | None, product: dict[str, Any]) -> dict[str, Any]:
    actual_title = f"{product.get('title', '')} {product.get('subtitle', '')}"
    authors = [row.get("name", "") for row in product.get("authors", [])]
    narrators = [row.get("name", "") for row in product.get("narrators", [])]
    candidate_seconds = float(product.get("runtime_length_min") or 0) * 60
    difference = abs(candidate_seconds - duration) / duration * 100 if duration and candidate_seconds else None
    title_match = normalized_identity(title) in normalized_identity(actual_title) or normalized_identity(actual_title) in normalized_identity(title)
    author_match = not author or any(normalized_identity(author) in normalized_identity(name) for name in authors)
    narrator_match = not narrator or any(normalized_identity(narrator) in normalized_identity(name) for name in narrators)
    language = str(product.get("language") or product.get("language_name") or "").casefold()
    language_match = language in {"en", "eng", "english"}
    unabridged = str(product.get("format_type") or "").casefold() != "abridged"
    score = (40 if title_match else 0) + (25 if author_match else 0) + (15 if narrator_match else -15) + (10 if language_match else -10) + (10 if unabridged else -25)
    if difference is not None:
        score += max(-20, 20 - difference * 4)
    strict = title_match and author_match and narrator_match and language_match and unabridged and (difference is None or difference <= 2)
    return {
        "titleMatch": title_match, "authorMatch": author_match, "narratorMatch": narrator_match,
        "language": language or None, "languageMatch": language_match, "unabridged": unabridged,
        "durationDifferencePercent": difference, "strictIdentityMatch": strict, "score": score,
    }


def command_audible_search(args: argparse.Namespace) -> int:
    products: list[dict[str, Any]] = []
    errors = []
    for region in args.regions.split(","):
        region = region.strip().lower()
        query = {"title": args.title, "num_results": args.limit, "products_sort_by": "Relevance", "response_groups": "contributors,media,product_desc,product_extended_attrs,sample"}
        if args.author:
            query["author"] = args.author
        url = audible_url(region, "/1.0/catalog/products", query)
        try:
            for product in json.loads(fetch(url, attempts=args.attempts, timeout=args.timeout))["products"]:
                products.append({"region": region, **product, "evidence": candidate_evidence(args.title, args.author, args.narrator, args.duration, product)})
        except Exception as error:
            errors.append({"region": region, "error": str(error)})
    products.sort(key=lambda row: row["evidence"]["score"], reverse=True)
    report = {"schemaVersion": 1, "generatedAt": utc_now(), "operation": "audible-search", "mutation": False, "query": {"title": args.title, "author": args.author, "narrator": args.narrator, "durationSeconds": args.duration}, "candidates": products, "errors": errors, "reviewNote": "Ranking is evidence only; a human must select the matching recording and edition."}
    write_json(args.report, report)
    print(args.report)
    return 1 if not products else 0


def command_audible_select(args: argparse.Namespace) -> int:
    source = args.candidates.resolve()
    protect_json_output(args.receipt, source)
    payload = load_json(source)
    candidates = payload.get("candidates", [])
    index = args.candidate - 1
    if index < 0 or index >= len(candidates):
        raise CuratorError(f"candidate must be between 1 and {len(candidates)}")
    selected = candidates[index]
    report = {
        "schemaVersion": 1, "generatedAt": utc_now(), "operation": "audible-select", "mutation": False,
        "candidateReport": str(source), "candidateNumber": args.candidate, "humanReviewed": True,
        "selected": selected, "reviewNote": args.note,
    }
    write_json(args.receipt, report)
    print(args.receipt)
    return 0


def curl_download(url: str, output: Path, args: argparse.Namespace) -> None:
    run(["curl", "--fail", "--location", "--silent", "--show-error", "--connect-timeout", str(args.connect_timeout), "--max-time", str(args.max_time), "--retry", str(args.retries), "--retry-all-errors", "--output", str(output), url], timeout=args.max_time * (args.retries + 1) + 30)


def command_audible_cache(args: argparse.Namespace) -> int:
    route = f"/1.0/catalog/products/{urllib.parse.quote(args.asin)}"
    url = audible_url(args.region, route, {"response_groups": "contributors,category_ladders,media,product_desc,product_extended_attrs,sample"})
    product = json.loads(fetch(url, attempts=args.attempts, timeout=args.timeout))["product"]
    cache = args.cache_dir.resolve() / f"{args.region}-{args.asin}"
    cache.mkdir(parents=True, exist_ok=True)
    product_path = cache / "product.json"
    protect_json_output(args.receipt, product_path, cache / "chapters.json")
    write_json(product_path, product)
    chapter_url = audible_url(args.region, f"/1.0/content/{urllib.parse.quote(args.asin)}/metadata", {"response_groups": "chapter_info"})
    chapter_path, chapter_error = cache / "chapters.json", None
    try:
        write_json(chapter_path, json.loads(fetch(chapter_url, attempts=args.attempts, timeout=args.timeout)))
    except Exception as error:
        chapter_path, chapter_error = None, str(error)
    image_url = (product.get("product_images") or {}).get("1000") or (product.get("product_images") or {}).get("500")
    artwork_path = None
    if image_url:
        artwork_path = cache / ("cover.png" if ".png" in image_url.lower() else "cover.jpg")
        curl_download(image_url, artwork_path, args)
    receipt = {"schemaVersion": 1, "generatedAt": utc_now(), "operation": "audible-cache", "mediaMutation": False, "asin": args.asin, "region": args.region, "product": str(product_path), "chapters": str(chapter_path) if chapter_path else None, "chapterError": chapter_error, "artwork": str(artwork_path) if artwork_path else None, "sourceUrls": {"product": url, "chapters": chapter_url, "artwork": image_url}}
    write_json(args.receipt, receipt)
    print(args.receipt)
    return 0


def clean_catalog_text(value: Any) -> str:
    text = re.sub(r"<br\s*/?>", "\n", str(value or ""), flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", html.unescape(text)).strip()


def command_metadata(args: argparse.Namespace) -> int:
    path = args.file.resolve()
    protect_json_output(args.receipt, path, args.product, args.artwork)
    product = load_json(args.product.resolve())
    authors = " & ".join(row.get("name", "") for row in product.get("authors", []) if row.get("name"))
    narrators = " & ".join(row.get("name", "") for row in product.get("narrators", []) if row.get("name"))
    title = args.title or product.get("title")
    metadata = {
        "title": title, "album": title, "artist": args.author or authors,
        "album_artist": args.author or authors, "composer": args.narrator or narrators,
        "date": args.year or product.get("release_date"),
        "comment": f"Narrated by {args.narrator or narrators}. Published by {product.get('publisher_name', '')}. Audible ASIN: {product.get('asin', '')}.",
        "description": clean_catalog_text(product.get("publisher_summary") or product.get("merchandising_summary")),
    }
    before_details = ffprobe(path)
    ensure_supported_auxiliary_streams(before_details)
    before_chapter_rows, _ = chapter_issues(before_details)
    before_chapters = len(before_chapter_rows)
    before_duration = float(before_details.get("format", {}).get("duration") or 0)
    before_audio_hashes = audio_hashes(path, before_details)
    before_audio = before_audio_hashes[0]
    plan = {"schemaVersion": 1, "generatedAt": utc_now(), "operation": "apply-metadata", "apply": args.apply, "file": str(path), "product": str(args.product.resolve()), "artwork": str(args.artwork.resolve()) if args.artwork else None, "metadata": metadata, "audioLanguage": args.language, "audioSha256Before": before_audio, "audioStreamHashesBefore": before_audio_hashes, "chapterCountBefore": before_chapters}
    if not args.apply:
        write_json(args.receipt, {**plan, "status": "planned"})
        print(args.receipt)
        return 0
    with tempfile.TemporaryDirectory(prefix=".audiobook-curator-metadata-", dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        command = ["ffmpeg", "-v", "error", "-xerror", "-i", str(path)]
        if args.artwork:
            command.extend(["-i", str(args.artwork.resolve())])
        # Preserve chapter-level title metadata while overriding reviewed global tags.
        if args.artwork:
            command.extend(["-map", "0:a?", "-map", "0:s?", "-map", "1:v:0", "-disposition:v:0", "attached_pic"])
        else:
            command.extend(["-map", "0:a?", "-map", "0:v?", "-map", "0:s?"])
        command.extend(["-map_chapters", "0", "-map_metadata", "0", "-c", "copy"])
        for key, value in metadata.items():
            if value not in (None, ""):
                command.extend(["-metadata", f"{key}={value}"])
        if args.language is not None:
            command.extend(["-metadata:s:a:0", f"language={args.language}"])
        command.extend(["-movflags", "+faststart", str(temporary)])
        run(command, timeout=args.timeout_hours * 3600)
        after_details = ffprobe(temporary)
        after_audio_hashes = audio_hashes(temporary, after_details)
        after_audio = after_audio_hashes[0]
        after_chapter_rows, _ = chapter_issues(after_details)
        after_chapters = len(after_chapter_rows)
        after_duration = float(after_details.get("format", {}).get("duration") or 0)
        if after_audio_hashes != before_audio_hashes:
            raise CuratorError("an audio stream changed during metadata update; original left untouched")
        if after_chapter_rows != before_chapter_rows or abs(after_duration - before_duration) > 0.01:
            raise CuratorError("chapter structure or duration changed during metadata update; original left untouched")
        after_tags = {str(key).casefold(): str(value) for key, value in after_details.get("format", {}).get("tags", {}).items()}
        missing_tags = [key for key, value in metadata.items() if value not in (None, "") and after_tags.get(key.casefold()) != str(value)]
        artwork_count = sum(1 for stream in after_details.get("streams", []) if stream.get("disposition", {}).get("attached_pic"))
        before_artwork_count = sum(1 for stream in before_details.get("streams", []) if stream.get("disposition", {}).get("attached_pic"))
        if missing_tags:
            raise CuratorError(f"metadata verification failed for: {', '.join(missing_tags)}; original left untouched")
        audio_stream = next((stream for stream in after_details.get("streams", []) if stream.get("codec_type") == "audio"), {})
        if args.language is not None and str(audio_stream.get("tags", {}).get("language") or "").casefold() != args.language.casefold():
            raise CuratorError("audio language metadata verification failed; original left untouched")
        if artwork_count < (1 if args.artwork else before_artwork_count):
            raise CuratorError("artwork verification failed; original left untouched")
        before_signature = stream_signature(before_details, include_artwork=not bool(args.artwork))
        after_signature = stream_signature(after_details, include_artwork=not bool(args.artwork))
        if before_signature != after_signature[:len(before_signature)]:
            raise CuratorError("non-artwork stream inventory changed; original left untouched")
        shutil.copystat(path, temporary)
        os.replace(temporary, path)
    receipt = {**plan, "status": "applied-verified", "audioSha256After": after_audio, "audioStreamHashesAfter": after_audio_hashes, "chapterCountAfter": after_chapters, "streamCountAfter": len(after_details.get("streams", [])), "artworkStreamsAfter": artwork_count, "verifiedMetadataKeys": [key for key, value in metadata.items() if value not in (None, "")] + (["audio.language"] if args.language is not None else []), "bytesAfter": path.stat().st_size, "sha256After": sha256(path)}
    write_json(args.receipt, receipt)
    print(args.receipt)
    return 0


def command_acoustic(args: argparse.Namespace) -> int:
    protect_json_output(args.receipt, args.file)
    try:
        from audiolocate import StreamMatcher
    except ImportError as error:
        raise CuratorError("Audiolocate is optional; install with: pip install -e '.[acoustic]'") from error
    product_url = audible_url(args.region, f"/1.0/catalog/products/{urllib.parse.quote(args.asin)}", {"response_groups": "contributors,media,product_desc,product_extended_attrs,sample"})
    product = json.loads(fetch(product_url, attempts=args.attempts, timeout=args.timeout))["product"]
    sample_url = product.get("sample_url")
    if not sample_url:
        raise CuratorError("Audible candidate has no sample URL")
    with tempfile.TemporaryDirectory(prefix="audiobook-curator-acoustic-") as directory:
        sample = Path(directory) / "sample.mp3"
        curl_download(sample_url, sample, args)
        result = StreamMatcher().find_match_from_sources(str(args.file.resolve()), str(sample), chunk_seconds=args.chunk_seconds, early_exit=True, verbose=args.verbose)
    receipt = {"schemaVersion": 1, "generatedAt": utc_now(), "operation": "audiolocate", "mutation": False, "file": str(args.file.resolve()), "asin": args.asin, "region": args.region, "audible": {"title": product.get("title"), "authors": [x.get("name") for x in product.get("authors", [])], "narrators": [x.get("name") for x in product.get("narrators", [])], "sampleUrl": sample_url}, "fingerprint": result, "verifiedRecording": bool(result.get("found"))}
    write_json(args.receipt, receipt)
    print(args.receipt)
    return 0 if receipt["verifiedRecording"] else 2


def whisper_text(payload: dict[str, Any]) -> str:
    transcription = payload.get("transcription")
    if isinstance(transcription, str):
        return transcription.strip()
    if isinstance(transcription, list):
        return " ".join(str(row.get("text", "")) for row in transcription).strip()
    return str(payload.get("result", {}).get("transcription", "")).strip()


def whisper_sampling_fractions(max_windows: int) -> list[float]:
    candidates = [0.05, 0.275, 0.5, 0.725, 0.95, 0.15, 0.85, 0.375, 0.625, 0.25, 0.75]
    return candidates[:max(5, min(max_windows, len(candidates)))]


def chapter_rows_from_payload(payload: Any, duration_seconds: float) -> list[dict[str, Any]]:
    chapter_data = payload
    if isinstance(payload, dict):
        if isinstance(payload.get("chapters"), list):
            chapter_data = payload["chapters"]
        else:
            chapter_data = payload.get("content_metadata", {}).get("chapter_info", {}).get("chapters")
    if not isinstance(chapter_data, list) or not chapter_data:
        raise CuratorError("chapter document contains no chapters")
    rows: list[dict[str, Any]] = []
    for index, source in enumerate(chapter_data, 1):
        if not isinstance(source, dict):
            raise CuratorError(f"chapter {index} is not an object")
        title = str(source.get("title") or source.get("name") or "").strip()
        if not title:
            raise CuratorError(f"chapter {index} has no title")
        if "startSeconds" in source:
            start = float(source["startSeconds"])
        else:
            start = float(source.get("start_offset_ms") or 0) / 1000
        if "endSeconds" in source:
            end = float(source["endSeconds"])
        elif source.get("length_ms") is not None:
            end = start + float(source["length_ms"]) / 1000
        else:
            end = -1
        rows.append({"title": title, "startSeconds": start, "endSeconds": end})
    for index, row in enumerate(rows):
        if row["endSeconds"] < 0:
            row["endSeconds"] = rows[index + 1]["startSeconds"] if index + 1 < len(rows) else duration_seconds
        if index + 1 < len(rows) and abs(row["endSeconds"] - rows[index + 1]["startSeconds"]) <= 1:
            row["endSeconds"] = rows[index + 1]["startSeconds"]
    if abs(rows[-1]["endSeconds"] - duration_seconds) <= 1:
        rows[-1]["endSeconds"] = duration_seconds
    for index, row in enumerate(rows, 1):
        if row["startSeconds"] < 0 or row["endSeconds"] <= row["startSeconds"]:
            raise CuratorError(f"chapter {index} has an invalid time range")
        if index > 1 and abs(row["startSeconds"] - rows[index - 2]["endSeconds"]) > 0.05:
            raise CuratorError(f"chapter boundary {index - 1}/{index} is discontinuous")
    if abs(rows[0]["startSeconds"]) > 0.1:
        raise CuratorError("first chapter does not start at zero")
    if abs(rows[-1]["endSeconds"] - duration_seconds) > 1:
        raise CuratorError("last chapter does not reach media end")
    return rows


def ffmetadata_for_chapters(rows: list[dict[str, Any]]) -> str:
    def escaped(value: str) -> str:
        return value.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", "\\n")
    lines = [";FFMETADATA1"]
    for row in rows:
        lines.extend([
            "[CHAPTER]", "TIMEBASE=1/1000",
            f"START={round(row['startSeconds'] * 1000)}", f"END={round(row['endSeconds'] * 1000)}",
            f"title={escaped(row['title'])}",
        ])
    return "\n".join(lines) + "\n"


def command_apply_chapters(args: argparse.Namespace) -> int:
    path = args.file.resolve()
    protect_json_output(args.receipt, path, args.chapters)
    before = ffprobe(path)
    ensure_supported_auxiliary_streams(before)
    duration = float(before.get("format", {}).get("duration") or 0)
    rows = chapter_rows_from_payload(load_json(args.chapters.resolve()), duration)
    before_audio_hashes = audio_hashes(path, before)
    before_audio = before_audio_hashes[0]
    plan = {
        "schemaVersion": 1, "generatedAt": utc_now(), "operation": "apply-chapters", "apply": args.apply,
        "file": str(path), "chapterDocument": str(args.chapters.resolve()), "chapters": rows,
        "chapterCountBefore": len(before.get("chapters", [])), "audioSha256Before": before_audio,
        "audioStreamHashesBefore": before_audio_hashes,
    }
    if not args.apply:
        write_json(args.receipt, {**plan, "status": "planned"})
        print(args.receipt)
        return 0
    with tempfile.TemporaryDirectory(prefix=".audiobook-curator-chapters-", dir=path.parent) as directory:
        work = Path(directory)
        metadata = work / "chapters.ffmetadata"
        metadata.write_text(ffmetadata_for_chapters(rows), encoding="utf-8")
        temporary = work / path.name
        run([
            "ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "ffmetadata", "-i", str(metadata),
            "-map", "0:a?", "-map", "0:v?", "-map", "0:s?", "-map_metadata", "0", "-map_chapters", "1", "-c", "copy", "-movflags", "+faststart", str(temporary),
        ], timeout=args.timeout_hours * 3600)
        after = ffprobe(temporary)
        after_audio_hashes = audio_hashes(temporary, after)
        after_audio = after_audio_hashes[0]
        after_rows, issues = chapter_issues(after)
        after_duration = float(after.get("format", {}).get("duration") or 0)
        actual_titles = [row["title"] for row in after_rows]
        boundary_mismatches = chapter_mapping_issues(rows, after_rows, check_titles=True, tolerance=0.05)
        if after_audio_hashes != before_audio_hashes:
            raise CuratorError("an audio stream changed during chapter update; original left untouched")
        if stream_signature(after) != stream_signature(before) or stable_format_tags(after) != stable_format_tags(before):
            raise CuratorError("non-chapter media state changed during chapter update; original left untouched")
        if abs(after_duration - duration) > 0.01 or len(after_rows) != len(rows) or actual_titles != [row["title"] for row in rows] or issues or boundary_mismatches:
            raise CuratorError(f"chapter verification failed; original left untouched: {issues}")
        shutil.copystat(path, temporary)
        os.replace(temporary, path)
    write_json(args.receipt, {
        **plan, "status": "applied-verified", "chapterCountAfter": len(after_rows),
        "audioSha256After": after_audio, "audioStreamHashesAfter": after_audio_hashes,
        "verifiedBoundaries": True, "durationSeconds": after_duration,
        "bytesAfter": path.stat().st_size, "sha256After": sha256(path),
    })
    print(args.receipt)
    return 0


def command_whisper(args: argparse.Namespace) -> int:
    protect_json_output(args.receipt, args.file, args.model)
    details = media_record(args.file.resolve())
    duration = details["durationSeconds"]
    windows = []
    fractions = whisper_sampling_fractions(args.max_windows)
    with tempfile.TemporaryDirectory(prefix="audiobook-curator-whisper-") as directory:
        work = Path(directory)
        for index, fraction in enumerate(fractions, 1):
            if index > 5 and sum(1 for row in windows if row["usable"]) >= 3:
                break
            start = max(0, min(duration - args.window_seconds, duration * fraction - args.window_seconds / 2))
            wav, base = work / f"window-{index}.wav", work / f"window-{index}"
            run(["ffmpeg", "-v", "error", "-xerror", "-ss", f"{start:.3f}", "-i", str(args.file.resolve()), "-t", str(args.window_seconds), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], timeout=300)
            run([args.whisper_cli, "-m", str(args.model.resolve()), "-l", args.language, "-t", str(args.threads), "-oj", "-of", str(base), "-np", str(wav)], timeout=args.timeout)
            text = whisper_text(load_json(Path(f"{base}.json")))
            windows.append({"index": index, "startSeconds": start, "sampleSeconds": args.window_seconds, "text": text, "usable": len(re.sub(r"\s+", " ", text)) >= args.minimum_chars})
    usable = sum(1 for row in windows if row["usable"])
    report = {"schemaVersion": 1, "generatedAt": utc_now(), "operation": "whisper-identity", "mutation": False, "file": str(args.file.resolve()), "model": str(args.model.resolve()), "requestedLanguage": args.language, "expectedTitle": args.title, "expectedAuthor": args.author, "windows": windows, "usableWindows": usable, "maxWindows": args.max_windows, "status": "transcript-ready" if usable >= 3 else "insufficient-spoken-windows", "review": "Confirm language, title/story identity, and narrator evidence from distributed excerpts; transcript text is evidence, not automatic proof."}
    write_json(args.receipt, report)
    print(args.receipt)
    return 0 if usable >= 3 else 2


def chapter_issues(details: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    duration = float(details.get("format", {}).get("duration") or 0)
    chapters, issues = [], []
    for index, chapter in enumerate(details.get("chapters", []), 1):
        row = {"number": index, "startSeconds": float(chapter.get("start_time") or 0), "endSeconds": float(chapter.get("end_time") or 0), "title": str(chapter.get("tags", {}).get("title") or "").strip()}
        chapters.append(row)
        if row["endSeconds"] <= row["startSeconds"]:
            issues.append(f"chapter {index} has non-positive duration")
        if not row["title"] or re.fullmatch(r"(?:unknown|untitled|track\s*\d*)", row["title"], re.I):
            issues.append(f"chapter {index} has a missing or placeholder title")
        if index > 1 and abs(row["startSeconds"] - chapters[-2]["endSeconds"]) > 0.05:
            issues.append(f"chapter boundary {index - 1}/{index} is discontinuous")
    if not chapters:
        issues.append("no chapters")
    else:
        if abs(chapters[0]["startSeconds"]) > 0.1:
            issues.append("first chapter does not start at zero")
        if abs(chapters[-1]["endSeconds"] - duration) > 1:
            issues.append("last chapter does not reach media end")
    return chapters, issues


def chapter_mapping_issues(expected: list[dict[str, Any]], actual: list[dict[str, Any]], *, check_titles: bool = True, tolerance: float = 0.15) -> list[str]:
    issues: list[str] = []
    if len(expected) != len(actual):
        issues.append(f"expected {len(expected)} source-mapped chapters, found {len(actual)}")
    for index, (wanted, found) in enumerate(zip(expected, actual), 1):
        if check_titles and wanted.get("title") != found.get("title"):
            issues.append(f"chapter {index} title does not match expected value")
        if abs(float(wanted.get("startSeconds") or 0) - float(found.get("startSeconds") or 0)) > tolerance:
            issues.append(f"chapter {index} start does not match expected boundary")
        if abs(float(wanted.get("endSeconds") or 0) - float(found.get("endSeconds") or 0)) > tolerance:
            issues.append(f"chapter {index} end does not match expected boundary")
    return issues


def command_audit(args: argparse.Namespace) -> int:
    path = args.file.resolve()
    protect_json_output(args.receipt, path, args.conversion_receipt)
    details = ffprobe(path)
    chapters, issues = chapter_issues(details)
    source_mapping = {"status": "not-requested", "issues": []}
    if args.conversion_receipt:
        conversion = load_json(args.conversion_receipt.resolve())
        expected = conversion.get("expectedChapters") or []
        mapping_issues = chapter_mapping_issues(expected, chapters)
        source_mapping = {"status": "verified" if not mapping_issues else "review-required", "conversionReceipt": str(args.conversion_receipt.resolve()), "issues": mapping_issues}
        issues.extend(f"source mapping: {issue}" for issue in mapping_issues)
    decode = "not-requested"
    if args.full_decode:
        try:
            run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a", "-f", "null", "-"], timeout=args.timeout_hours * 3600)
            decode = "verified"
        except Exception as error:
            decode = "failed"
            issues.append(f"full decode failed: {error}")
    stat = path.stat()
    report = {"schemaVersion": 1, "generatedAt": utc_now(), "operation": "audit", "mutation": False, "file": str(path), "bytes": stat.st_size, "sha256": sha256(path), "audioSha256": audio_sha256(path), "probe": media_record(path), "chapters": chapters, "chapterIssues": issues, "sourceChapterMapping": source_mapping, "fullDecode": decode, "status": "verified" if not issues and (not args.full_decode or decode == "verified") else "review-required"}
    write_json(args.receipt, report)
    print(args.receipt)
    return 0 if report["status"] == "verified" else 2


def add_network_bounds(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=30)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audiobook-curator", description="Plan-first, receipt-backed audiobook curation",
        epilog="Exit 0: completed; exit 1: operational/validation failure; exit 2: review required or inconclusive evidence.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    inventory = sub.add_parser("inventory", help="Probe source audio without changing it")
    inventory.add_argument("source", type=Path); inventory.add_argument("--report", required=True, type=Path); inventory.add_argument("--strict", action="store_true"); inventory.set_defaults(func=command_inventory)
    library = sub.add_parser("library-audit", help="Audit metadata, artwork, chapters, duplicate candidates, and multipart groups")
    library.add_argument("sources", nargs="+", type=Path); library.add_argument("--report", required=True, type=Path); library.add_argument("--concurrency", type=int, choices=range(1, 9), default=2); library.add_argument("--strict", action="store_true"); library.set_defaults(func=command_library_audit)
    select = sub.add_parser("select", help="Choose the strongest source among normalized collisions")
    select.add_argument("--inventory", required=True, type=Path); select.add_argument("--report", required=True, type=Path); select.set_defaults(func=command_select)
    convert = sub.add_parser("convert", help="Plan or apply conversion to a single chaptered M4B")
    convert.add_argument("--selection", required=True, type=Path); convert.add_argument("--output", required=True, type=Path); convert.add_argument("--receipt", required=True, type=Path); convert.add_argument("--title", required=True); convert.add_argument("--author", required=True); convert.add_argument("--narrator"); convert.add_argument("--year"); convert.add_argument("--language", help="Explicit reviewed ISO audio language; omitted makes no language claim"); convert.add_argument("--artwork", type=Path); convert.add_argument("--engine", choices=["ffmpeg", "audiobook-forge"], default="ffmpeg", help="Multipart conversion engine; a single M4B is always stream-copied"); convert.add_argument("--forge-cli", default="audiobook-forge"); convert.add_argument("--forge-aac-encoder", default="auto"); convert.add_argument("--audio-bitrate", default="128k", help="ffmpeg engine AAC bitrate"); convert.add_argument("--timeout-hours", type=int, default=8); convert.add_argument("--apply", action="store_true"); convert.add_argument("--overwrite", action="store_true", help="Allow replacement only when used with --apply"); convert.set_defaults(func=command_convert)
    search = sub.add_parser("audible-search", help="Search and rank Audible identity candidates")
    search.add_argument("--title", required=True); search.add_argument("--author"); search.add_argument("--narrator"); search.add_argument("--duration", type=float); search.add_argument("--regions", default="us"); search.add_argument("--limit", type=int, default=20); search.add_argument("--report", required=True, type=Path); add_network_bounds(search); search.set_defaults(func=command_audible_search)
    audible_select = sub.add_parser("audible-select", help="Record an explicit human-reviewed Audible candidate choice")
    audible_select.add_argument("--candidates", required=True, type=Path); audible_select.add_argument("--candidate", required=True, type=int, help="One-based candidate number from audible-search"); audible_select.add_argument("--note"); audible_select.add_argument("--receipt", required=True, type=Path); audible_select.set_defaults(func=command_audible_select)
    cache = sub.add_parser("audible-cache", help="Cache one reviewed Audible candidate and artwork")
    cache.add_argument("--asin", required=True); cache.add_argument("--region", choices=sorted(AUDIBLE_HOSTS), default="us"); cache.add_argument("--cache-dir", required=True, type=Path); cache.add_argument("--receipt", required=True, type=Path); cache.add_argument("--connect-timeout", type=int, default=15); cache.add_argument("--max-time", type=int, default=90); cache.add_argument("--retries", type=int, default=3); add_network_bounds(cache); cache.set_defaults(func=command_audible_cache)
    metadata = sub.add_parser("apply-metadata", help="Plan or atomically apply reviewed catalog metadata and artwork")
    metadata.add_argument("--file", required=True, type=Path); metadata.add_argument("--product", required=True, type=Path); metadata.add_argument("--artwork", type=Path); metadata.add_argument("--title"); metadata.add_argument("--author"); metadata.add_argument("--narrator"); metadata.add_argument("--year"); metadata.add_argument("--language", help="Explicit ISO audio language; omitted preserves the current value"); metadata.add_argument("--receipt", required=True, type=Path); metadata.add_argument("--timeout-hours", type=int, default=2); metadata.add_argument("--apply", action="store_true"); metadata.set_defaults(func=command_metadata)
    chapters = sub.add_parser("apply-chapters", help="Plan or atomically apply reviewed chapters without transcoding audio")
    chapters.add_argument("--file", required=True, type=Path); chapters.add_argument("--chapters", required=True, type=Path); chapters.add_argument("--receipt", required=True, type=Path); chapters.add_argument("--timeout-hours", type=int, default=2); chapters.add_argument("--apply", action="store_true"); chapters.set_defaults(func=command_apply_chapters)
    acoustic = sub.add_parser("acoustic-verify", help="Match a bounded Audible sample against local audio")
    acoustic.add_argument("--file", required=True, type=Path); acoustic.add_argument("--asin", required=True); acoustic.add_argument("--region", choices=sorted(AUDIBLE_HOSTS), default="us"); acoustic.add_argument("--receipt", required=True, type=Path); acoustic.add_argument("--chunk-seconds", type=int, default=900); acoustic.add_argument("--connect-timeout", type=int, default=15); acoustic.add_argument("--max-time", type=int, default=90); acoustic.add_argument("--retries", type=int, default=3); acoustic.add_argument("--verbose", action="store_true"); add_network_bounds(acoustic); acoustic.set_defaults(func=command_acoustic)
    whisper = sub.add_parser("whisper-verify", help="Transcribe distributed windows for language and identity review")
    whisper.add_argument("--file", required=True, type=Path); whisper.add_argument("--model", required=True, type=Path); whisper.add_argument("--whisper-cli", default="whisper-cli"); whisper.add_argument("--language", default="en"); whisper.add_argument("--title"); whisper.add_argument("--author"); whisper.add_argument("--window-seconds", type=int, default=35); whisper.add_argument("--minimum-chars", type=int, default=80); whisper.add_argument("--threads", type=int, default=4); whisper.add_argument("--timeout", type=int, default=600); whisper.add_argument("--max-windows", type=int, choices=range(5, 12), default=9); whisper.add_argument("--receipt", required=True, type=Path); whisper.set_defaults(func=command_whisper)
    audit = sub.add_parser("audit", help="Validate metadata, chapters, hashes, and optional full decode")
    audit.add_argument("--file", required=True, type=Path); audit.add_argument("--conversion-receipt", type=Path, help="Verify chapter titles and cumulative boundaries against a conversion receipt"); audit.add_argument("--receipt", required=True, type=Path); audit.add_argument("--full-decode", action="store_true"); audit.add_argument("--timeout-hours", type=int, default=8); audit.set_defaults(func=command_audit)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except (CuratorError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
