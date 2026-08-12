---
name: curate-audiobooks
description: Safely inventory, compare, identify, convert, enrich, and validate audiobook audio as a single M4B. Use for source-quality selection, Audible candidate review, Audiolocate acoustic confirmation, Whisper language or identity checks, artwork and chapter construction, metadata cleanup, filesystem-safe naming, full-decode integrity checks, SHA-256 receipts, or reusable audiobook audit reports.
---

# Curate audiobooks

Use the bundled `audiobook-curator` CLI. Resolve the plugin root for the active client, then run its installed executable or `python3 -m audiobook_curator.cli` after bootstrap.

## Guardrails

- Treat source audio as immutable. Never delete, rename, or overwrite an original.
- Start with `inventory` and `select`. Read their JSON before proposing a conversion.
- Keep evidence gates distinct: catalog metadata is a candidate, acoustic matching supports edition identity, and Whisper excerpts support language/story/narrator review.
- Require the user to approve the candidate identity and conversion destination before using `--apply`.
- Omit `--apply` for a dry-run conversion plan. Do not use `--overwrite` unless the exact existing derived output is confirmed replaceable.
- Remove apostrophes and unsafe characters from output filenames only. Retain correct punctuation in embedded title, author, narrator, descriptions, and chapter titles.
- Finish with `audit --full-decode`; report file SHA-256, audio-stream SHA-256, byte size, chapter findings, and decode status separately.
- Do not infer media-server, database, or listening-history work. This skill has no such integration.

## Workflow

1. Run `inventory SOURCE --report receipts/inventory.json`. Review probe errors, duration, codecs, bitrates, existing artwork, and chapters.
2. Run `select --inventory receipts/inventory.json --report receipts/selection.json`. Confirm natural ordering and every selected-vs-alternate decision. Edit the selection receipt only when human evidence overrides the deterministic quality ranking.
3. Establish identity. Run `audible-search` with title, author, and source duration. Review the top candidates; do not auto-accept rank one.
4. After a candidate is accepted, run `audible-cache` to retain product, chapter, and artwork evidence. Downloads are bounded and retried.
5. When edition ambiguity matters, run `acoustic-verify`. A positive match is strong same-recording evidence; a negative match is inconclusive if samples differ by intro, mastering, or region.
6. When language, narrator, or story identity is uncertain, run `whisper-verify` across distributed windows. Read at least three usable excerpts and record the human conclusion.
7. Run `convert` without `--apply` and inspect the plan. Confirm output name, embedded punctuation, source order, metadata, artwork, and destination.
8. Re-run the exact command with `--apply`. Conversion always creates derived media and retains originals.
9. Run `audit --full-decode`. Do not call the book verified if chapters have gaps, non-positive ranges, placeholder titles, the last chapter misses the media end, the decode fails, or hashes/size are absent.
10. Summarize the candidate decision, acoustic and speech evidence, conversion receipt, chapter audit, integrity result, and unresolved limitations.

Read [references/evidence-gates.md](references/evidence-gates.md) when identity is ambiguous or a result is being promoted as verified. Read [references/dependencies.md](references/dependencies.md) for bootstrap and platform requirements.
