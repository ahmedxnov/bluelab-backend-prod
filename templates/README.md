# Report templates

The candidate report PDF is **HTML rendered by headless Chromium** (C-9,
ADR-0021), so the template is a versioned artifact of this repository — like
`../sql/`, and for the same reason: it is not Python, but it is not incidental
either. `bluelab.work.render_report` renders it; `bluelab.adapters.pdf_render`
drives Chromium.

`report/` holds the template. One artifact serves **both** HR and the candidate
(api/02 §4) — there is no second, softer version.

## The three properties the template must hold

**1. Concealment-safe by construction, not by post-filtering.**

The render worker may read the candidate row, graded attempts with their
scorecards and moments, the position, and the drills' **participant-safe
projection only**. It must not read `drill_concealed` or rubric weights, and the
manager's `internal_note` is excluded the same way (api/02 §4, FR-HIR-012).

The template snapshot tests are where this is asserted: no internal note, no
weights, no challenges, no hidden motives (AC-HIR-004, quality/06 §5). A filter
applied after rendering would be the wrong shape — the data must never arrive.

**2. Commentary English, quoted speech verbatim Arabic, correct RTL.**

Same rule as the screen (FR-SCR-008). The PDF keeps **Noto Naskh / Noto Kufi
Arabic** — a print register, locked by ADR-0021 and deliberately different from
the screen's IBM Plex Sans Arabic. Fonts are embedded, from `../assets/fonts/`.

**3. Document accessibility is inherited from this HTML.**

Headings, real table semantics, and `lang` tags in the template are what the PDF's
accessibility comes from. Tagged-PDF fidelity is verified at Phase 14 with the
same template snapshot tests (ux/06 §5).

## The pinned token seam

ADR-0042 is explicit that tokens are declared **once**, as CSS custom properties,
and consumed by both the SPA and these templates — *"one token source styles SPA
and PDF, so the report's on-screen and printed forms stay visually kin without
duplicated style constants."*

The frontend repository exports `src/styles/theme.css` as its versioned token
artifact. This repository vendors that artifact at `report/design-tokens.css` and
records the frontend source repository, source commit, source path, and SHA-256 in
`report/design-tokens.lock.json`. `python tools/verify_design_tokens.py` fails if
the local bytes drift. Updating the tokens is an explicit reviewed copy-and-lock
change; builds never read a sibling checkout or network location.
