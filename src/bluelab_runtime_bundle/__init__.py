"""The runtime-bundle contract — the *only* package shared with the call plane.

Canonical copy lives in `Implementation/bluelab-agent-prod/src/bluelab_runtime_bundle/`;
this is the version-locked mirror ADR-0071 requires across the two deployment
units. Divergence between the two is a defect, not a variation.

Nothing in `bluelab.modules` or `bluelab.work` may import this package — only
`bluelab.calls.bundle_builder` may, and the import-linter contract in
`pyproject.toml` enforces that.
"""
