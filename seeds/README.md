# Seeds

Two kinds, and the distinction is load-bearing (data/04 §6):

* **Platform reference** — `legal_document_version`, `authoring_option`, `badge`.
  Idempotent upserts with fixed literal UUIDs, environment-invariant. These ship
  as *data migrations* in the Alembic lineage.
* **The v1 vertical seed** — an org with a team and its product documents. This is
  **customer data**, created through ops provisioning and the product's own flows
  in whatever environment needs it. It is never a SQL fixture, in any environment.

The seed CLI is part of the timed bootstrap: `docker compose up` +
`alembic upgrade head` + seed must complete in **under 10 minutes** from a clean
clone, and that figure is a commit-gate stage (infra/00 §4, pipeline/02 §2 row 11).
