---
description: Audit an audiobook's identity, chapters, hashes, and decode integrity
argument-hint: <path-to-audiobook>
disable-model-invocation: true
---

Use the `curate-audiobooks` skill to audit `$ARGUMENTS`. Begin read-only. Inventory the file, inspect embedded metadata and chapters, and ask before any conversion or metadata mutation. If the user requests a final integrity claim, run a full decode and include byte size plus file and audio-stream SHA-256 receipts.
