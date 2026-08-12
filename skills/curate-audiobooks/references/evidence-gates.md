# Evidence gates

Keep these claims separate in receipts and reports:

| Gate | Supports | Does not prove |
|---|---|---|
| ffprobe inventory | readable container metadata and stream facts | full media integrity |
| deterministic quality score | a reproducible source preference | perceptual superiority or correct edition |
| Audible catalog candidate | plausible title, contributors, duration, and artwork | same recording |
| Audiolocate positive match | the reference sample occurs in local audio | complete-file integrity or metadata correctness |
| Whisper distributed excerpts | spoken language and reviewable identity clues | exact edition by itself |
| chapter audit | ordered, titled, continuous chapter structure | decoded audio integrity |
| full decode | all selected audio packets decode without ffmpeg error | semantic identity |
| SHA-256 and bytes | exact output identity for later verification | source equivalence unless compared to a prior receipt |

Use duration difference as a ranking signal, not a rejection rule. Regional catalog entries, intros, credits, and mastering can differ.

Treat an Audiolocate negative as inconclusive when the sample may be absent, replaced, or too short. Use distributed speech windows and contributor evidence before rejecting an edition.

Chapter validation requires positive durations, natural order, non-placeholder titles, continuity within 50 ms, a start near zero, and an end within one second of media duration. Where one chapter is generated per source file, also compare boundaries to cumulative source duration.
