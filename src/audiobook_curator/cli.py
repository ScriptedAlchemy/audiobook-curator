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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


AUDIO_EXTENSIONS = {".aac", ".aif", ".aiff", ".flac", ".m4a", ".m4b", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
AUDIBLE_HOSTS = {
    "us": "api.audible.com", "uk": "api.audible.co.uk", "ca": "api.audible.ca",
    "au": "api.audible.com.au", "fr": "api.audible.fr", "de": "api.audible.de",
    "jp": "api.audible.co.jp", "it": "api.audible.it", "in": "api.audible.in",
    "es": "api.audible.es",
}
USER_AGENT = "audiobook-curator/0.1 (+https://github.com/ScriptedAlchemy/audiobook-curator)"


class CuratorError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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


def audio_sha256(path: Path) -> str:
    result = run([
        "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy",
        "-f", "hash", "-hash", "sha256", "-",
    ], timeout=7200)
    return result.stdout.strip().removeprefix("SHA256=")


def media_record(path: Path, root: Path | None = None) -> dict[str, Any]:
    details = ffprobe(path)
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
    return (lossless, int(row.get("bitRate") or 0), int(row.get("sampleRate") or 0), int(row.get("bytes") or 0))


def command_inventory(args: argparse.Namespace) -> int:
    source = args.source.resolve()
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
    inventory = load_json(args.inventory)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in inventory["files"]:
        grouped.setdefault(collision_key(row), []).append(row)
    selections = []
    for key, candidates in sorted(grouped.items()):
        ordered = sorted(candidates, key=quality_score, reverse=True)
        selections.append({"identityKey": key, "selected": ordered[0], "alternates": ordered[1:], "reason": "lossless, then bitrate, sample rate, and byte size"})
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
    plan = {
        "schemaVersion": 1, "generatedAt": utc_now(), "operation": "convert", "apply": args.apply,
        "sourcesPreserved": True, "inputs": [str(path.resolve()) for path in inputs], "output": str(output),
        "embeddedMetadata": {"title": args.title, "album": args.title, "artist": args.author, "album_artist": args.author, "composer": args.narrator, "date": args.year, "language": args.language},
        "filenamePolicy": "apostrophes removed from filesystem name; embedded punctuation retained",
    }
    if not args.apply:
        write_json(args.receipt, {**plan, "status": "planned"})
        print(args.receipt)
        return 0
    if output.exists() and not args.overwrite:
        raise CuratorError(f"output exists; refusing to overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    common_root = Path(os.path.commonpath([str(path.parent) for path in inputs]))
    with tempfile.TemporaryDirectory(prefix="audiobook-curator-") as directory:
        work = Path(directory)
        concat = work / "concat.txt"
        concat.write_text("".join(f"file '{str(path.resolve()).replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n" for path in inputs), encoding="utf-8")
        metadata_path = work / "metadata.txt"
        metadata = {"title": args.title, "album": args.title, "artist": args.author, "album_artist": args.author, "composer": args.narrator, "date": args.year, "language": args.language}
        metadata_path.write_text(ffmetadata(inputs, common_root, metadata), encoding="utf-8")
        temporary = work / "output.m4b"
        command = ["ffmpeg", "-v", "error", "-xerror", "-f", "concat", "-safe", "0", "-i", str(concat), "-f", "ffmetadata", "-i", str(metadata_path)]
        if args.artwork:
            command.extend(["-i", str(args.artwork.resolve())])
        command.extend(["-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1"])
        if args.artwork:
            command.extend(["-map", "2:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"])
        command.extend(["-c:a", "aac", "-b:a", args.audio_bitrate, "-movflags", "+faststart", str(temporary)])
        run(command, timeout=args.timeout_hours * 3600, capture=True)
        before_audio = audio_sha256(temporary)
        shutil.copy2(temporary, output)
    result = media_record(output)
    receipt = {**plan, "status": "converted", "outputBytes": output.stat().st_size, "outputSha256": sha256(output), "audioSha256": before_audio, "probe": result}
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


def candidate_evidence(title: str, author: str | None, duration: float | None, product: dict[str, Any]) -> dict[str, Any]:
    actual_title = f"{product.get('title', '')} {product.get('subtitle', '')}"
    authors = [row.get("name", "") for row in product.get("authors", [])]
    candidate_seconds = float(product.get("runtime_length_min") or 0) * 60
    difference = abs(candidate_seconds - duration) / duration * 100 if duration and candidate_seconds else None
    title_match = normalized_identity(title) in normalized_identity(actual_title) or normalized_identity(actual_title) in normalized_identity(title)
    author_match = not author or any(normalized_identity(author) in normalized_identity(name) for name in authors)
    score = (40 if title_match else 0) + (25 if author_match else 0) + (10 if str(product.get("format_type", "")).casefold() != "abridged" else -25)
    if difference is not None:
        score += max(-20, 20 - difference * 4)
    return {"titleMatch": title_match, "authorMatch": author_match, "durationDifferencePercent": difference, "score": score}


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
                products.append({"region": region, **product, "evidence": candidate_evidence(args.title, args.author, args.duration, product)})
        except Exception as error:
            errors.append({"region": region, "error": str(error)})
    products.sort(key=lambda row: row["evidence"]["score"], reverse=True)
    report = {"schemaVersion": 1, "generatedAt": utc_now(), "operation": "audible-search", "mutation": False, "query": {"title": args.title, "author": args.author, "durationSeconds": args.duration}, "candidates": products, "errors": errors}
    write_json(args.report, report)
    print(args.report)
    return 1 if not products else 0


def curl_download(url: str, output: Path, args: argparse.Namespace) -> None:
    run(["curl", "--fail", "--location", "--silent", "--show-error", "--connect-timeout", str(args.connect_timeout), "--max-time", str(args.max_time), "--retry", str(args.retries), "--retry-all-errors", "--output", str(output), url], timeout=args.max_time * (args.retries + 1) + 30)


def command_audible_cache(args: argparse.Namespace) -> int:
    route = f"/1.0/catalog/products/{urllib.parse.quote(args.asin)}"
    url = audible_url(args.region, route, {"response_groups": "contributors,category_ladders,media,product_desc,product_extended_attrs,sample"})
    product = json.loads(fetch(url, attempts=args.attempts, timeout=args.timeout))["product"]
    cache = args.cache_dir.resolve() / f"{args.region}-{args.asin}"
    cache.mkdir(parents=True, exist_ok=True)
    product_path = cache / "product.json"
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
    product = load_json(args.product.resolve())
    authors = " & ".join(row.get("name", "") for row in product.get("authors", []) if row.get("name"))
    narrators = " & ".join(row.get("name", "") for row in product.get("narrators", []) if row.get("name"))
    title = args.title or product.get("title")
    metadata = {
        "title": title, "album": title, "artist": args.author or authors,
        "album_artist": args.author or authors, "composer": args.narrator or narrators,
        "date": args.year or product.get("release_date"), "publisher": product.get("publisher_name"),
        "comment": f"Narrated by {args.narrator or narrators}. Audible ASIN: {product.get('asin', '')}.",
        "description": clean_catalog_text(product.get("publisher_summary") or product.get("merchandising_summary")),
        "language": args.language,
    }
    before_details = ffprobe(path)
    before_chapters = len(before_details.get("chapters", []))
    before_duration = float(before_details.get("format", {}).get("duration") or 0)
    before_audio = audio_sha256(path)
    plan = {"schemaVersion": 1, "generatedAt": utc_now(), "operation": "apply-metadata", "apply": args.apply, "file": str(path), "product": str(args.product.resolve()), "artwork": str(args.artwork.resolve()) if args.artwork else None, "metadata": metadata, "audioSha256Before": before_audio, "chapterCountBefore": before_chapters}
    if not args.apply:
        write_json(args.receipt, {**plan, "status": "planned"})
        print(args.receipt)
        return 0
    with tempfile.TemporaryDirectory(prefix="audiobook-curator-metadata-") as directory:
        temporary = Path(directory) / path.name
        command = ["ffmpeg", "-v", "error", "-xerror", "-i", str(path)]
        if args.artwork:
            command.extend(["-i", str(args.artwork.resolve())])
        # Preserve chapter-level title metadata while overriding reviewed global tags.
        command.extend(["-map", "0:a:0", "-map_chapters", "0", "-map_metadata", "0", "-c:a", "copy"])
        if args.artwork:
            command.extend(["-map", "1:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"])
        for key, value in metadata.items():
            if value not in (None, ""):
                command.extend(["-metadata", f"{key}={value}"])
        command.extend(["-movflags", "+faststart", str(temporary)])
        run(command, timeout=args.timeout_hours * 3600)
        after_details = ffprobe(temporary)
        after_audio = audio_sha256(temporary)
        after_chapters = len(after_details.get("chapters", []))
        after_duration = float(after_details.get("format", {}).get("duration") or 0)
        if after_audio != before_audio:
            raise CuratorError("audio stream changed during metadata update; original left untouched")
        if after_chapters != before_chapters or abs(after_duration - before_duration) > 0.01:
            raise CuratorError("chapter count or duration changed during metadata update; original left untouched")
        os.replace(temporary, path)
    receipt = {**plan, "status": "applied-verified", "audioSha256After": after_audio, "chapterCountAfter": after_chapters, "bytesAfter": path.stat().st_size, "sha256After": sha256(path)}
    write_json(args.receipt, receipt)
    print(args.receipt)
    return 0


def command_acoustic(args: argparse.Namespace) -> int:
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


def command_whisper(args: argparse.Namespace) -> int:
    details = media_record(args.file.resolve())
    duration = details["durationSeconds"]
    windows = []
    fractions = [0.05, 0.275, 0.5, 0.725, 0.95]
    with tempfile.TemporaryDirectory(prefix="audiobook-curator-whisper-") as directory:
        work = Path(directory)
        for index, fraction in enumerate(fractions, 1):
            start = max(0, min(duration - args.window_seconds, duration * fraction - args.window_seconds / 2))
            wav, base = work / f"window-{index}.wav", work / f"window-{index}"
            run(["ffmpeg", "-v", "error", "-xerror", "-ss", f"{start:.3f}", "-i", str(args.file.resolve()), "-t", str(args.window_seconds), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], timeout=300)
            run([args.whisper_cli, "-m", str(args.model.resolve()), "-l", args.language, "-t", str(args.threads), "-oj", "-of", str(base), "-np", str(wav)], timeout=args.timeout)
            text = whisper_text(load_json(Path(f"{base}.json")))
            windows.append({"index": index, "startSeconds": start, "sampleSeconds": args.window_seconds, "text": text, "usable": len(re.sub(r"\s+", " ", text)) >= args.minimum_chars})
    usable = sum(1 for row in windows if row["usable"])
    report = {"schemaVersion": 1, "generatedAt": utc_now(), "operation": "whisper-identity", "mutation": False, "file": str(args.file.resolve()), "model": str(args.model.resolve()), "requestedLanguage": args.language, "expectedTitle": args.title, "expectedAuthor": args.author, "windows": windows, "usableWindows": usable, "status": "transcript-ready" if usable >= 3 else "insufficient-spoken-windows", "review": "Confirm language, title/story identity, and narrator evidence from distributed excerpts; transcript text is evidence, not automatic proof."}
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


def command_audit(args: argparse.Namespace) -> int:
    path = args.file.resolve()
    details = ffprobe(path)
    chapters, issues = chapter_issues(details)
    decode = "not-requested"
    if args.full_decode:
        try:
            run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"], timeout=args.timeout_hours * 3600)
            decode = "verified"
        except Exception as error:
            decode = "failed"
            issues.append(f"full decode failed: {error}")
    stat = path.stat()
    report = {"schemaVersion": 1, "generatedAt": utc_now(), "operation": "audit", "mutation": False, "file": str(path), "bytes": stat.st_size, "sha256": sha256(path), "audioSha256": audio_sha256(path), "probe": media_record(path), "chapters": chapters, "chapterIssues": issues, "fullDecode": decode, "status": "verified" if not issues and (not args.full_decode or decode == "verified") else "review-required"}
    write_json(args.receipt, report)
    print(args.receipt)
    return 0 if report["status"] == "verified" else 2


def add_network_bounds(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=30)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audiobook-curator", description="Plan-first, receipt-backed audiobook curation")
    sub = parser.add_subparsers(dest="command", required=True)
    inventory = sub.add_parser("inventory", help="Probe source audio without changing it")
    inventory.add_argument("source", type=Path); inventory.add_argument("--report", required=True, type=Path); inventory.add_argument("--strict", action="store_true"); inventory.set_defaults(func=command_inventory)
    select = sub.add_parser("select", help="Choose the strongest source among normalized collisions")
    select.add_argument("--inventory", required=True, type=Path); select.add_argument("--report", required=True, type=Path); select.set_defaults(func=command_select)
    convert = sub.add_parser("convert", help="Plan or apply conversion to a single chaptered M4B")
    convert.add_argument("--selection", required=True, type=Path); convert.add_argument("--output", required=True, type=Path); convert.add_argument("--receipt", required=True, type=Path); convert.add_argument("--title", required=True); convert.add_argument("--author", required=True); convert.add_argument("--narrator"); convert.add_argument("--year"); convert.add_argument("--language", default="eng"); convert.add_argument("--artwork", type=Path); convert.add_argument("--audio-bitrate", default="128k"); convert.add_argument("--timeout-hours", type=int, default=8); convert.add_argument("--apply", action="store_true"); convert.add_argument("--overwrite", action="store_true", help="Allow replacement only when used with --apply"); convert.set_defaults(func=command_convert)
    search = sub.add_parser("audible-search", help="Search and rank Audible identity candidates")
    search.add_argument("--title", required=True); search.add_argument("--author"); search.add_argument("--duration", type=float); search.add_argument("--regions", default="us"); search.add_argument("--limit", type=int, default=20); search.add_argument("--report", required=True, type=Path); add_network_bounds(search); search.set_defaults(func=command_audible_search)
    cache = sub.add_parser("audible-cache", help="Cache one reviewed Audible candidate and artwork")
    cache.add_argument("--asin", required=True); cache.add_argument("--region", choices=sorted(AUDIBLE_HOSTS), default="us"); cache.add_argument("--cache-dir", required=True, type=Path); cache.add_argument("--receipt", required=True, type=Path); cache.add_argument("--connect-timeout", type=int, default=15); cache.add_argument("--max-time", type=int, default=90); cache.add_argument("--retries", type=int, default=3); add_network_bounds(cache); cache.set_defaults(func=command_audible_cache)
    metadata = sub.add_parser("apply-metadata", help="Plan or atomically apply reviewed catalog metadata and artwork")
    metadata.add_argument("--file", required=True, type=Path); metadata.add_argument("--product", required=True, type=Path); metadata.add_argument("--artwork", type=Path); metadata.add_argument("--title"); metadata.add_argument("--author"); metadata.add_argument("--narrator"); metadata.add_argument("--year"); metadata.add_argument("--language", default="eng"); metadata.add_argument("--receipt", required=True, type=Path); metadata.add_argument("--timeout-hours", type=int, default=2); metadata.add_argument("--apply", action="store_true"); metadata.set_defaults(func=command_metadata)
    acoustic = sub.add_parser("acoustic-verify", help="Match a bounded Audible sample against local audio")
    acoustic.add_argument("--file", required=True, type=Path); acoustic.add_argument("--asin", required=True); acoustic.add_argument("--region", choices=sorted(AUDIBLE_HOSTS), default="us"); acoustic.add_argument("--receipt", required=True, type=Path); acoustic.add_argument("--chunk-seconds", type=int, default=900); acoustic.add_argument("--connect-timeout", type=int, default=15); acoustic.add_argument("--max-time", type=int, default=90); acoustic.add_argument("--retries", type=int, default=3); acoustic.add_argument("--verbose", action="store_true"); add_network_bounds(acoustic); acoustic.set_defaults(func=command_acoustic)
    whisper = sub.add_parser("whisper-verify", help="Transcribe distributed windows for language and identity review")
    whisper.add_argument("--file", required=True, type=Path); whisper.add_argument("--model", required=True, type=Path); whisper.add_argument("--whisper-cli", default="whisper-cli"); whisper.add_argument("--language", default="en"); whisper.add_argument("--title"); whisper.add_argument("--author"); whisper.add_argument("--window-seconds", type=int, default=35); whisper.add_argument("--minimum-chars", type=int, default=80); whisper.add_argument("--threads", type=int, default=4); whisper.add_argument("--timeout", type=int, default=600); whisper.add_argument("--receipt", required=True, type=Path); whisper.set_defaults(func=command_whisper)
    audit = sub.add_parser("audit", help="Validate metadata, chapters, hashes, and optional full decode")
    audit.add_argument("--file", required=True, type=Path); audit.add_argument("--receipt", required=True, type=Path); audit.add_argument("--full-decode", action="store_true"); audit.add_argument("--timeout-hours", type=int, default=8); audit.set_defaults(func=command_audit)
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
