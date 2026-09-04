# Embedded fonts for PDF rendering

**Noto Naskh Arabic** and **Noto Kufi Arabic**, embedded in the Chromium render
container (C-9, ADR-0021).

Two reasons they live here rather than being fetched:

* **Correctness.** A missing Arabic face renders verbatim quoted speech as tofu
  boxes in the artifact that goes to HR and defends a hiring decision. The render
  container has no business depending on a font CDN at render time.
* **Isolation.** The render sandbox runs with network isolation and metadata
  egress denied (SEC-016 / SEC-025, ADR-0055), so it could not fetch them anyway.

These are deliberately **not** the screen's fonts. The SPA uses the IBM Plex
superfamily including IBM Plex Sans Arabic (ADR-0041); the PDF keeps Noto because
print is a different register, and ADR-0021 locked that before the screen language
was chosen. The two are meant to differ.

Latin faces come from the same token layer as the SPA — see `../../templates/README.md`
for the open question about how the token source crosses the repository boundary.
