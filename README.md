# audiobook-curator

`audiobook-curator` is a reusable Claude Code and Codex plugin for turning audiobook sources into verified single-file M4Bs without sacrificing the originals. It packages the operational workflow and a portable CLI; it contains no personal media, credentials, run history, media-server integration, or machine-specific paths.

The governing idea is simple: discovery is not identity, identity is not integrity, and a successful conversion is not a verified result. Each gate gets its own JSON receipt.

## What it covers

- recursive source inventory with ffprobe facts
- deterministic quality selection across normalized filename collisions
- natural source order and one chapter per selected input
- dry-run conversion plans; `--apply` is required to create media
- single AAC/M4B output while retaining all source files
- filesystem-safe output names with apostrophes removed, while embedded metadata retains correct punctuation
- Audible multi-region candidate search, scoring, response caching, artwork, and chapter metadata
- bounded/retrying downloads (`curl` has connect timeout, total timeout, and retry limits)
- optional Audiolocate sample-to-local acoustic verification
- optional whisper.cpp transcription at 5%, 27.5%, 50%, 72.5%, and 95% for language and identity review
- chapter title, order, positive-duration, continuity, first-boundary, and media-end validation
- optional full decode plus file SHA-256, audio-stream SHA-256, and byte-size receipts

## Safety model

`inventory`, `select`, `audible-search`, `audible-cache`, `acoustic-verify`, `whisper-verify`, and `audit` never alter source media. They may write only the report or cache path explicitly supplied.

`convert` is a dry run unless `--apply` is present. It never deletes or renames inputs. It refuses to replace an existing derived output unless `--overwrite` is also explicit. Keep plans, applied receipts, and final audits together so future checks can reproduce exactly what was selected and emitted.

## Dependencies

- Python 3.11+
- ffmpeg and ffprobe
- curl
- optional: `audiolocate`
- optional: `whisper-cli` from whisper.cpp and a user-supplied ggml model

Bootstrap from a clone:

```bash
./scripts/bootstrap.sh
```

The script checks system executables, creates an isolated `.venv`, and installs the Python package there in editable mode. It does not alter the system Python, install ffmpeg, download models, or modify an AI client. `bin/audiobook-curator` automatically uses that environment.

## CLI walkthrough

Inventory and select sources:

```bash
audiobook-curator inventory ./source-book --report receipts/inventory.json
audiobook-curator select \
  --inventory receipts/inventory.json \
  --report receipts/selection.json
```

Find and cache a reviewed identity candidate:

```bash
audiobook-curator audible-search \
  --title "Gerald's Game" \
  --author "Stephen King" \
  --duration 11160 \
  --regions us,uk \
  --report receipts/audible-candidates.json

audiobook-curator audible-cache \
  --asin EXAMPLEASIN \
  --region us \
  --cache-dir cache/audible \
  --receipt receipts/audible-cache.json
```

Optional edition and speech checks:

```bash
./.venv/bin/python -m pip install -e '.[acoustic]'
audiobook-curator acoustic-verify \
  --file ./candidate.m4b \
  --asin EXAMPLEASIN \
  --receipt receipts/acoustic.json

audiobook-curator whisper-verify \
  --file ./candidate.m4b \
  --model ./models/ggml-small.bin \
  --title "Gerald's Game" \
  --author "Stephen King" \
  --receipt receipts/whisper.json
```

Plan application of the reviewed cached product to an existing derived M4B, then repeat with `--apply`. The update is staged to a temporary file and promoted only if the audio-stream hash, duration, and chapter count remain unchanged:

```bash
audiobook-curator apply-metadata \
  --file ./candidate.m4b \
  --product cache/audible/us-EXAMPLEASIN/product.json \
  --artwork cache/audible/us-EXAMPLEASIN/cover.jpg \
  --title "Gerald's Game" \
  --receipt receipts/metadata-plan.json
```

Plan the conversion first. The apostrophe remains in the embedded title even though it is absent from the output filename:

```bash
audiobook-curator convert \
  --selection receipts/selection.json \
  --output "./library/Geralds Game.m4b" \
  --title "Gerald's Game" \
  --author "Stephen King" \
  --narrator "Example Narrator" \
  --artwork cache/audible/us-EXAMPLEASIN/cover.jpg \
  --receipt receipts/conversion-plan.json
```

After reviewing the plan, repeat it with `--apply` and a new receipt path. Then run the final gate:

```bash
audiobook-curator audit \
  --file "./library/Geralds Game.m4b" \
  --full-decode \
  --receipt receipts/final-audit.json
```

## Install in Codex

Add the GitHub repository as a marketplace and install the plugin:

```bash
codex plugin marketplace add ScriptedAlchemy/audiobook-curator --ref main
codex plugin add audiobook-curator@audiobook-curator-marketplace
```

Start a new Codex thread, then invoke `$curate-audiobooks` or ask naturally for audiobook curation. The first workflow execution may ask you to run `./scripts/bootstrap.sh` in a clone so the local CLI and system dependencies are available.

For development without installing it into the active client, validate the checked-out plugin with the commands in [Development](#development).

## Install in Claude Code

Add the same repository as a marketplace and install at user scope:

```bash
claude plugin marketplace add ScriptedAlchemy/audiobook-curator --scope user
claude plugin install audiobook-curator@audiobook-curator-marketplace --scope user
```

Restart or run `/reload-plugins`. Invoke `/audiobook-curator:curate-audiobooks` or the explicit `/audiobook-curator:audit-audiobook` shortcut.

For a local development checkout, use:

```bash
claude --plugin-dir ./audiobook-curator
```

## Receipt meanings

| Receipt | Claim |
|---|---|
| inventory | selected files are probe-readable and their stream facts were recorded |
| selection | the quality ranking is reproducible and alternates remain visible |
| Audible candidate/cache | catalog identity evidence was retained, not automatically accepted |
| acoustic | the catalog sample did or did not match local audio |
| Whisper | distributed excerpts are available for human language and story review |
| conversion | the exact output, size, and hashes produced from preserved sources |
| final audit | chapter structure, hashes, and optional full decode passed or need review |

## Development

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/python -m pytest -q
./.venv/bin/python scripts/validate_repository.py
./.venv/bin/python scripts/fixture_workflow.py
python3 /path/to/plugin-creator/scripts/validate_plugin.py .
python3 /path/to/skill-creator/scripts/quick_validate.py skills/curate-audiobooks
claude plugin validate .
```

The last three commands require the corresponding client/tooling to be installed. CI runs the portable test suite, a synthetic two-chapter ffmpeg workflow, and repository privacy/manifest fences.

## Limitations

- Audible endpoints used here are not a supported API contract for this project and can change or rate-limit requests.
- Candidate scoring ranks evidence; it never auto-accepts an edition.
- A positive acoustic sample is strong same-recording evidence, but not proof of complete-file integrity. A negative match can be inconclusive.
- Whisper results require human review and depend on the selected model, language hint, and the amount of spoken content in each window.
- Conversion currently emits AAC at a configurable bitrate. Source ordering and chapter construction are deterministic, but exact audio preservation is not claimed after transcoding.
- Artwork embedding depends on ffmpeg accepting the supplied image for the MP4 container.
- Chapter titles default to source-relative filenames. Correct editorial titles may require a reviewed selection or later metadata pass.

## License

MIT
