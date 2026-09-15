# Grading fixture boundary

This directory accepts synthetic roleplays and explicitly consented study material admitted through `docs/grading-evidence-sourcing.md`. Each admitted fixture has an intake source code, a content hash, an authorization reference, permitted-use and retention fields, and a withdrawal procedure before a test consumes it.

Production transcripts, recordings, candidate data, and customer content are prohibited. Tests use synthetic identifiers, and fixture filenames contain no person or customer name.

`corpus.v1.json` is the fixed ten-attempt pilot set. It covers all four call types, regional and register variation, and English and Arabic transcript-injection probes. Every item is marked completed and carries its frozen scenario, answer key, weighted rubric, provenance, authorization, coverage tags, content hash, and transcript hash. Each injection probe also carries a hashed paired-control transcript that removes only the buyer's injected instruction while preserving every participant utterance and transcript timestamp.

The expert-evidence template is deliberately non-passing until a real blind panel supplies pre-registered thresholds, three or more distinct qualified raters, and every fixture/rater score. It is a collection shape, never substitute evidence.
