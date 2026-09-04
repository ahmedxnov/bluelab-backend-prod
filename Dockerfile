# The server image (pipeline/02 §4.1).
#
# Built EXACTLY ONCE, from a green `main`, then scanned, SBOM'd, signed, and pushed
# to ECR by digest. From that point the digest IS the release: promotion re-pins it
# and nothing downstream ever rebuilds (ADR-0065). A rebuild between staging and
# prod would void the verification (infra/00 §2 rule 3).
#
# One image serves the application-plane, work-plane, and sweeper entrypoints. The
# call plane is NOT in this image — it is a separate deployment unit that holds no
# data-plane credential (ADR-0071).
#
# Required properties, each with an owner:
#   * non-root, read-only root filesystem where the runtime allows; the Chromium
#     report-render worker gets its writable scratch mount (infra/02 §4);
#   * the render sandbox has metadata egress denied and network isolation
#     (SEC-016 / SEC-025, ADR-0055);
#   * a health endpoint the deploy gates can poll (pipeline/04 §3);
#   * base-image and lockfile digests pinned and verified — no external API
#     credential is ever baked in (SEC-023).
#
# ---------------------------------------------------------------------------
# STAGE build   — resolve and install dependencies from the verified lockfile
# STAGE runtime — non-root, read-only rootfs, entrypoint selected by command
# ---------------------------------------------------------------------------
#
# Scaffold only: the stages above are described, not yet written.
