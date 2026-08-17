---
name: curate-audiobooks
description: Safely inventory, compare, identify, convert, enrich, and validate audiobook audio as a single M4B. Use for source-quality selection, Audible candidate review, Audiolocate acoustic confirmation, Whisper language or identity checks, artwork and chapter construction, metadata cleanup, filesystem-safe naming, full-decode integrity checks, SHA-256 receipts, or reusable audiobook audit reports.
---

# Curate audiobooks

Use the bundled CLI at `<plugin-root>/bin/audiobook-curator`. The plugin root is two directories above this `SKILL.md`. Run `<plugin-root>/scripts/bootstrap.sh` once if its isolated environment is absent; do not substitute an unrelated global executable.

## Guardrails

- Treat source audio as immutable. Never delete, rename, or overwrite an original. `apply-metadata` and `apply-chapters` may update only a separately identified, user-approved derived M4B.
- Start with `inventory` and `select`. Read their JSON before proposing a conversion.
- Keep evidence gates distinct: catalog metadata is a candidate, acoustic matching supports edition identity, and Whisper excerpts support language/story/narrator review.
- Require the user to approve the candidate identity and conversion destination before using `--apply`.
- Omit `--apply` for a dry-run conversion plan. Do not use `--overwrite` unless the exact existing derived output is confirmed replaceable.
- Remove apostrophes and unsafe characters from output filenames only. Retain correct punctuation in embedded title, author, narrator, descriptions, and chapter titles.
- Finish with `audit --full-decode`; report file SHA-256, audio-stream SHA-256, byte size, chapter findings, and decode status separately.
- Do not infer media-server, database, or listening-history work. This skill has no such integration.

## Workflow

1. For a library or holding folder, run `library-audit SOURCE --report receipts/library-audit.json`. Treat duplicate and multipart groups only as review leads.
2. Run `inventory SOURCE --report receipts/inventory.json`. Review probe errors, duration, codecs, bit depth, sample rate, bitrate, layout, existing artwork, and chapters.
3. Run `select --inventory receipts/inventory.json --report receipts/selection.json`. Confirm natural ordering, duration-conflict warnings, and every selected-vs-alternate decision. Edit the selection receipt only when human evidence overrides the deterministic quality ranking.
4. Establish identity. Run `audible-search` with title, author, narrator when known, and source duration. Review the top candidates; do not auto-accept rank one. Record the explicit choice with `audible-select`.
5. After a candidate is accepted, run `audible-cache` to retain product, chapter, and artwork evidence. Downloads are bounded and retried.
6. When edition ambiguity matters, run `acoustic-verify`. A positive match is strong same-recording evidence; a negative match is inconclusive if samples differ by intro, mastering, or region.
7. When language, narrator, or story identity is uncertain, run `whisper-verify`. It begins with five distributed windows and samples more positions when fewer than three contain usable speech. Read at least three usable excerpts and record the human conclusion.
8. Run `convert` without `--apply` and inspect the plan. Confirm output name, embedded punctuation, source order, metadata, artwork, engine, and destination. Prefer `--engine audiobook-forge` when available for multipart sources. A single existing M4B is stream-copied automatically.
9. Re-run the exact conversion with `--apply`. Conversion always creates derived media and retains originals. Run `audit --conversion-receipt ...` now to prove source-title/order/boundary mapping.
10. Plan and apply `apply-metadata` to the derived output using the reviewed cached product and artwork. Verify the receipt lists every applied tag and the expected artwork count.
11. If catalog chapters match this exact recording, plan and apply `apply-chapters` using the reviewed chapter JSON. This stream-copies audio and verifies the complete result. Otherwise retain the source-derived chapters.
12. Run the final `audit --full-decode` without a conversion receipt if editorial chapter titles replaced source filenames. Do not call the book verified if chapters have gaps, non-positive ranges, placeholder titles, the last chapter misses the media end, the decode fails, or hashes/size are absent.
13. Summarize the candidate decision, acoustic and speech evidence, conversion receipt, metadata/chapter receipts, integrity result, and unresolved limitations.

Read [references/evidence-gates.md](references/evidence-gates.md) when identity is ambiguous or a result is being promoted as verified. Read [references/dependencies.md](references/dependencies.md) for bootstrap and platform requirements.
