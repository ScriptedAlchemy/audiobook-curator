# Dependencies

Required:

- Python 3.11 or newer
- `ffmpeg` and `ffprobe`
- `curl` for bounded binary downloads

Optional:

- `audiolocate` for acoustic sample matching (`pip install -e '.[acoustic]'`)
- `whisper-cli` from whisper.cpp plus a local ggml model for distributed-window transcription

Run `scripts/bootstrap.sh` from the repository root to verify system tools and install the CLI in editable mode. The plugin never downloads a Whisper model automatically.

Audible catalog endpoints are unauthenticated but unofficial for this project. Their response shape and availability can change. Cache the reviewed response and record the region and ASIN.
