# Frozen-snapshot version fixtures

One frozen example per snapshot version `v`, forever (data/04 §5).

Frozen rows are never rewritten, so snapshot evolution is **reader-side only**: a
format change bumps `v` for newly published drills and there is no backfill, ever.
Every reader — brief, product reference, grading, PDF — must accept every
historical `v`, and this fixture set is what proves it in CI. A change that cannot
be read compatibly is not a snapshot change; it is a new-drill-content feature.
