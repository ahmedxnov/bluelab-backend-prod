"""The build-gate tools, attacked (pipeline/02 §2, quality/08 §2).

Every script in `tools/` is a stage that blocks a merge, which makes each one a
single point of failure for the property it guards. A gate that cannot fail is
indistinguishable from no gate, and it fails *quietly* — the build stays green and
nobody looks again. So each check here is shown going red on the exact defect it
exists to catch, and green on the near-miss it must not flag.

The near-miss half matters as much as the breach. A scan that flags the rep
performance band as a rollout-tier branch gets switched off within a week, taking
the real check with it — so `PRODUCT_TIER_NAMES` gets a test of its own.

None of this touches a database or a network: these are pure source and document
analysis, so they belong at L1 (tests/README).
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

TOOLS = Path(__file__).resolve().parents[2] / "tools"
BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS))

conformance = importlib.import_module("check_conformance_diff")
coverage = importlib.import_module("check_requirement_coverage")
tiers = importlib.import_module("scan_tier_branches")
cookies = importlib.import_module("audit_cookie_attributes")
ratchet = importlib.import_module("_ratchet")
mutation = importlib.import_module("check_mutation_score")

pytestmark = [pytest.mark.l1_unit, pytest.mark.build_gate]


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def kinds(findings) -> set[str]:
    return {f.kind for f in findings}


# ── the ratchet ───────────────────────────────────────────────────────────────


@pytest.fixture
def baseline_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(ratchet, "BASELINE_DIR", tmp_path)
    return tmp_path


def test_a_missing_baseline_forgives_nothing(baseline_dir):
    """A ratchet that started life accepting whatever it found would forgive the
    entire backlog on its first run and never mention it again."""
    verdict = ratchet.evaluate("probe", {"a", "b"})

    assert verdict.new == ("a", "b")
    assert verdict.clean is False


def test_a_new_gap_fails(baseline_dir):
    ratchet.write("probe", {"a"}, note="")
    verdict = ratchet.evaluate("probe", {"a", "b"})

    assert verdict.new == ("b",)
    assert verdict.clean is False


def test_a_closed_gap_fails_so_the_baseline_tightens(baseline_dir):
    """The half that makes this a ratchet rather than a permanent excuse list.

    Without it the file only grows stale, and every entry it still names stays
    forgiven forever — including one that gets re-introduced after being fixed.
    """
    ratchet.write("probe", {"a", "b"}, note="")
    verdict = ratchet.evaluate("probe", {"a"})

    assert verdict.resolved == ("b",)
    assert verdict.clean is False


def test_an_unchanged_gap_set_is_clean(baseline_dir):
    ratchet.write("probe", {"a", "b"}, note="")

    assert ratchet.evaluate("probe", {"a", "b"}).clean


def test_the_report_names_both_directions_and_the_fix(baseline_dir):
    """The rendered message is the whole interface for whoever hits this in CI.

    It has to distinguish a regression from a baseline gone slack — the two need
    opposite responses — and name the command that resolves it, or the reader is
    left guessing which of the two situations they are in.
    """
    ratchet.write("probe", {"stale"}, note="")
    lines = ratchet.render("probe", ratchet.evaluate("probe", {"fresh"}), noun="thing(s)")
    report = "\n".join(lines)

    assert "+ fresh" in report and "regression" in report
    assert "- stale" in report and "slack" in report
    assert "--update-baseline" in report


def test_a_satisfied_ratchet_renders_nothing(baseline_dir):
    ratchet.write("probe", {"a"}, note="")

    assert ratchet.render("probe", ratchet.evaluate("probe", {"a"}), noun="thing(s)") == []


# ── the conformance diff ──────────────────────────────────────────────────────


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """Write a synthetic error catalog and point the checker at it."""

    def _write(rows: dict[str, int]) -> None:
        body = "\n".join(f"| `{slug}` | {status} | when | no |" for slug, status in rows.items())
        write(tmp_path / "catalog.md", f"| `type` slug | Status | When | Retry? |\n|---|---|---|---|\n{body}\n")
        monkeypatch.setattr(conformance, "ERROR_CATALOG", tmp_path / "catalog.md")
        monkeypatch.setattr(conformance, "MIN_CATALOG_ROWS", 1)

    return _write


@pytest.fixture
def registry(monkeypatch):
    """Swap the real problem registry for a synthetic one."""
    from bluelab.platform.errors import catalog as real

    def _set(rows: dict[str, int]) -> None:
        fake = real._Registry()
        for slug, status in rows.items():
            fake.add(slug, status, slug)
        monkeypatch.setattr(real, "REGISTRY", fake)

    return _set


def test_a_problem_the_catalog_never_specified_is_a_contradiction(catalog, registry):
    """A client branches on `type` alone (ux/05 §3), so a problem nobody documented
    is one no client knows how to render."""
    catalog({"not-found": 404})
    registry({"not-found": 404, "surprise": 418})

    result = conformance.check_problems()

    assert kinds(result.contradictions) == {"problem-uncontracted"}
    assert result.contradictions[0].subject == "surprise"


def test_a_status_disagreement_is_a_contradiction(catalog, registry):
    """The defect this found for real on its first run: `subject-unknown` was 422
    in the registry and 404 in the contract."""
    catalog({"subject-unknown": 404})
    registry({"subject-unknown": 422})

    result = conformance.check_problems()

    assert kinds(result.contradictions) == {"problem-status"}
    assert "404" in result.contradictions[0].detail


def test_an_unimplemented_problem_is_a_gap_not_a_contradiction(catalog, registry):
    """Absence is the normal state of a part-built product, so it rides the ratchet
    instead of blocking every merge."""
    catalog({"not-found": 404, "method-not-allowed": 405})
    registry({"not-found": 404})

    result = conformance.check_problems()

    assert result.contradictions == []
    assert result.gaps == {"problem:method-not-allowed"}


def test_a_collapsed_catalog_parse_fails_loudly(tmp_path, monkeypatch):
    """If the document's table shape moves, the diff would compare against almost
    nothing and report it as clean."""
    write(tmp_path / "catalog.md", "no tables here\n")
    monkeypatch.setattr(conformance, "ERROR_CATALOG", tmp_path / "catalog.md")

    with pytest.raises(ValueError, match="parsed only 0 registry rows"):
        conformance.documented_problems()


def test_an_app_that_fails_to_import_is_not_treated_as_serving_nothing(monkeypatch):
    """The false green this gate must refuse.

    Zero served operations matches a baseline that already forgives all of them, so
    a broken import would pass silently. It has to raise instead.
    """
    monkeypatch.setattr(conformance, "APP_MODULE", "bluelab.entrypoints._nonexistent_probe")

    with pytest.raises(ValueError, match="could not be imported"):
        conformance.served_operations()


def test_the_app_factory_is_found_not_just_a_module_level_instance(monkeypatch):
    """`entrypoints.api` exposes a factory and no module-level `app`.

    Regression: this check originally looked only for `app`, so when that
    attribute was removed it went straight back to reporting "the application
    plane is still scaffold" — with three operations actually being served. It
    passed, silently, having stopped reading the server.
    """
    module = types.ModuleType("bluelab.entrypoints._factory_probe")
    paths = {"/api/v1/probe": {"get": {"operationId": "probe"}}}
    module.create_app = lambda: types.SimpleNamespace(openapi=lambda: {"paths": paths})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bluelab.entrypoints._factory_probe", module)
    monkeypatch.setattr(conformance, "APP_MODULE", "bluelab.entrypoints._factory_probe")

    served, reason = conformance.served_operations()

    assert served == {("GET", "/api/v1/probe"): "probe"}
    assert reason == ""


def test_a_factory_that_raises_is_not_treated_as_serving_nothing(monkeypatch):
    """Same false green as a failed import, one level down. The factory resolves
    Settings, so a missing environment variable is the likely cause — and it must
    not read as an empty surface that matches the baseline."""
    module = types.ModuleType("bluelab.entrypoints._broken_factory")

    def _boom() -> None:
        raise RuntimeError("VALKEY_URL is not set")

    module.create_app = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bluelab.entrypoints._broken_factory", module)
    monkeypatch.setattr(conformance, "APP_MODULE", "bluelab.entrypoints._broken_factory")

    with pytest.raises(ValueError, match="raised RuntimeError"):
        conformance.served_operations()


def test_a_module_exposing_neither_is_an_honest_gap(monkeypatch):
    """No factory and no instance means the surface genuinely is not built yet.

    That is a *gap* — reported, baselined, not fatal — and it must stay
    distinguishable from the two failures above, where something exists and broke.
    Asserted against a scaffold-shaped probe module rather than against the real
    entrypoint, which now serves; a test pinned to "the app does not exist yet"
    only holds until it does.
    """
    module = types.ModuleType("bluelab.entrypoints._empty_probe")
    monkeypatch.setitem(sys.modules, "bluelab.entrypoints._empty_probe", module)
    monkeypatch.setattr(conformance, "APP_MODULE", "bluelab.entrypoints._empty_probe")

    served, reason = conformance.served_operations()

    assert served is None
    assert "scaffold" in reason


def test_a_malformed_contract_exits_two_not_one(tmp_path, monkeypatch):
    """A broken `openapi.yaml` is the likeliest way this check ever fails to run.

    Exit 1 would be indistinguishable from real drift and would point the reader at
    the server instead of at the file that is actually broken. `yaml.YAMLError` is
    not a subclass of ValueError or OSError, so it has to be caught by name.
    """
    write(tmp_path / "bad.yaml", "paths:\n  - [unclosed\n")
    monkeypatch.setattr(conformance, "CONTRACT", tmp_path / "bad.yaml")
    monkeypatch.setattr(sys, "argv", ["check_conformance_diff.py"])

    assert conformance.main() == 2


@pytest.fixture
def serving(monkeypatch):
    """Install a stub application module that serves a given operation set.

    A stub module rather than a patched `served_operations`, so the real
    introspection path runs — `importlib`, the `app` attribute lookup, `openapi()`,
    and the method filtering. Patching the function instead would leave every line
    this gate depends on untested while the tests looked thorough.

    These branches cannot be reached any other way today: nothing is served, so
    without a stub the most important code in the tool never executes.
    """

    def _serve(operations: dict[tuple[str, str], str]) -> None:
        paths: dict[str, dict[str, object]] = {}
        for (method, path), operation_id in operations.items():
            paths.setdefault(path, {})[method.lower()] = {"operationId": operation_id}

        module = types.ModuleType("bluelab.entrypoints._stub_app")
        module.app = types.SimpleNamespace(openapi=lambda: {"paths": paths})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bluelab.entrypoints._stub_app", module)
        monkeypatch.setattr(conformance, "APP_MODULE", "bluelab.entrypoints._stub_app")

    return _serve


def test_a_served_path_the_contract_lacks_is_a_contradiction(serving):
    """The contract moves first (ADR-0035 decision 3). A route that appeared without
    an amendment is drift, not progress."""
    serving({("POST", "/api/v1/invented"): "invented"})

    result = conformance.check_operations()

    assert "operation-uncontracted" in kinds(result.contradictions)
    assert any(f.subject == "POST /api/v1/invented" for f in result.contradictions)


def test_a_renamed_operation_id_is_a_contradiction(serving):
    """The generated TypeScript client is named from `operationId`, so a rename
    breaks every caller (ADR-0013) while the path still looks correct."""
    serving({("POST", "/api/v1/calls"): "startTheCall"})

    result = conformance.check_operations()

    assert "operation-id" in kinds(result.contradictions)
    detail = next(f.detail for f in result.contradictions if f.kind == "operation-id")
    assert "startTheCall" in detail


def test_a_correctly_served_operation_is_neither_gap_nor_contradiction(serving):
    contracted = conformance.contract_operations()
    serving({("POST", "/api/v1/calls"): contracted[("POST", "/api/v1/calls")]})

    result = conformance.check_operations()

    assert result.contradictions == []
    assert "operation:POST /api/v1/calls" not in result.gaps
    assert "operation:GET /api/v1/positions" in result.gaps, "the rest are still gaps"


def test_the_real_contract_parses_into_operations():
    """The regexes and the YAML shape must match the file as committed, not merely
    reject an empty one."""
    operations = conformance.contract_operations()

    assert len(operations) > 80
    assert ("POST", "/api/v1/calls") in operations
    assert all(operation_id for operation_id in operations.values()), "every operation needs an operationId"


# ── requirement coverage ──────────────────────────────────────────────────────


@pytest.fixture
def spec_world(tmp_path, monkeypatch):
    """A miniature spec set plus a test tree, wired into the coverage checker."""

    def _build(*, declared_ids: list[str], test_source: str) -> None:
        headings = "\n".join(f"### {i}\nbody\n" for i in declared_ids if not i.startswith("FR-"))
        rows = "\n".join(f"| {i} | realized by something |" for i in declared_ids if i.startswith("FR-"))
        write(tmp_path / "specs" / "01.md", headings)
        write(tmp_path / "specs" / "02.md", "")
        write(tmp_path / "api" / "06.md", f"| FR | Realized by |\n|---|---|\n{rows}\n")
        write(tmp_path / "tests" / "test_probe.py", test_source)

        monkeypatch.setattr(coverage, "SPEC_NFR_CMP", tmp_path / "specs" / "01.md")
        monkeypatch.setattr(coverage, "SPEC_SEC", tmp_path / "specs" / "02.md")
        monkeypatch.setattr(coverage, "API_FR_COVERAGE", tmp_path / "api" / "06.md")
        monkeypatch.setattr(coverage, "TESTS", tmp_path / "tests")
        monkeypatch.setattr(coverage, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(coverage, "MIN_DECLARED", 1)
        monkeypatch.setattr(coverage, "citable", lambda: frozenset(declared_ids))

    return _build


def test_a_live_tag_covers_its_requirement(spec_world):
    spec_world(
        declared_ids=["SEC-001"],
        test_source='import pytest\n\n@pytest.mark.verifies("SEC-001")\ndef test_x(): pass\n',
    )

    contradictions, gaps, _ = coverage.run()

    assert contradictions == []
    assert gaps == set()


@pytest.mark.parametrize("mark", ["skip", "xfail", "skipif(True, reason='x')"])
def test_a_skipped_or_xfail_test_does_not_count_as_coverage(spec_world, mark):
    """ADR-0062, mechanised: a placeholder cannot fail, and a test that cannot fail
    proves nothing."""
    spec_world(
        declared_ids=["SEC-001"],
        test_source=(
            f"import pytest\n\n"
            f"@pytest.mark.{mark}\n"
            f'@pytest.mark.verifies("SEC-001")\n'
            f"def test_x(): pass\n"
        ),
    )

    _, gaps, _ = coverage.run()

    assert gaps == {"SEC-001"}


def test_a_module_level_skip_silences_the_whole_file(spec_world):
    """The shape of an accidentally-quarantined suite still reporting its
    requirements as covered."""
    spec_world(
        declared_ids=["SEC-001"],
        test_source=(
            "import pytest\n\n"
            "pytestmark = [pytest.mark.skip]\n\n"
            '@pytest.mark.verifies("SEC-001")\n'
            "def test_x(): pass\n"
        ),
    )

    _, gaps, _ = coverage.run()

    assert gaps == {"SEC-001"}


def test_a_tag_naming_no_declared_requirement_is_a_contradiction(spec_world):
    """Worse than an untested requirement, because it inflates the number."""
    spec_world(
        declared_ids=["SEC-001"],
        test_source='import pytest\n\n@pytest.mark.verifies("SEC-999")\ndef test_x(): pass\n',
    )

    contradictions, gaps, _ = coverage.run()

    assert len(contradictions) == 1
    assert "SEC-999" in contradictions[0]
    assert gaps == {"SEC-001"}, "the real requirement is still uncovered"


def test_a_collapsed_declaration_parse_fails_loudly(tmp_path, monkeypatch):
    write(tmp_path / "empty.md", "nothing\n")
    for attribute in ("SPEC_NFR_CMP", "SPEC_SEC", "API_FR_COVERAGE"):
        monkeypatch.setattr(coverage, attribute, tmp_path / "empty.md")

    with pytest.raises(ValueError, match="parsed only 0 requirement declarations"):
        coverage.declared()


def test_the_real_spec_set_declares_every_family():
    """The parser must match the documents as committed."""
    requirements = coverage.declared()
    families = {r.split("-")[0] for r in requirements}

    assert families == {"FR", "NFR", "CMP", "SEC"}
    assert len(requirements) > 150


def test_acceptance_criteria_and_adrs_are_citable_but_not_counted():
    """Tests cite `AC-IDA-006` and `ADR-0023`; those are real identifiers that must
    validate, but quality/08 §5 tracks the four requirement families."""
    universe = coverage.citable()
    requirements = coverage.declared()

    assert "AC-IDA-006" in universe
    assert "ADR-0023" in universe
    assert "AC-IDA-006" not in requirements
    assert "ADR-0023" not in requirements


# ── the tier scan ─────────────────────────────────────────────────────────────


def test_a_branch_on_a_tier_name_is_flagged(tmp_path):
    write(tmp_path / "m.py", "def f(s):\n    if s.rollout_tier:\n        return 1\n    return 2\n")

    assert kinds(tiers.scan_python(tmp_path)) == {"tier-branch"}


def test_a_tier_literal_is_flagged_whatever_the_variable_is_called(tmp_path):
    """The one that catches a rung smuggled in under an innocent name."""
    write(tmp_path / "m.py", "def f(env):\n    if env.rung == 'tier 1':\n        return 1\n    return 2\n")

    assert kinds(tiers.scan_python(tmp_path)) == {"tier-literal"}


def test_prose_about_tiers_is_not_a_branch(tmp_path):
    """Reading the syntax tree rather than the text is what makes this true —
    telemetry/correlation.py carries the tier as a metric label, which SEC-026
    explicitly permits."""
    write(
        tmp_path / "m.py",
        '"""At Tier 1 the cap is tighter, and the tier label rides every metric."""\n'
        "TIER_LABEL = 'tier'\n"
        "def f(x):\n    if x:\n        return 1\n    return 2\n",
    )

    assert tiers.scan_python(tmp_path) == []


def test_the_rep_performance_band_is_not_a_rollout_branch(tmp_path):
    """A scan that flagged V-4's score band would be switched off within a week,
    taking the real check with it."""
    write(
        tmp_path / "m.py",
        "def band(performance_tier):\n    if performance_tier == 'top':\n        return 1\n    return 2\n",
    )

    assert tiers.scan_python(tmp_path) == []


@pytest.mark.verifies("SEC-026")
def test_the_committed_source_carries_no_tier_branch():
    """SEC-026's own verification clause, executed: "a code scan finds no
    tier-conditional branch"."""
    assert tiers.scan_python(tiers.BACKEND_SRC) == []
    assert tiers.scan_settings() == [], "Settings must expose no tier at all"


# ── the cookie audit ──────────────────────────────────────────────────────────


@pytest.fixture
def cookie_world(tmp_path, monkeypatch):
    """A synthetic source tree with a stand-in cookie module."""

    def _build(*, module_source: str, other_source: str = "") -> None:
        module = write(tmp_path / "pkg" / "cookies.py", module_source)
        if other_source:
            write(tmp_path / "pkg" / "routes.py", other_source)
        monkeypatch.setattr(cookies, "BACKEND_SRC", tmp_path)
        monkeypatch.setattr(cookies, "COOKIE_MODULE", module)

    return _build


CONFORMING = (
    'SESSION_COOKIE = "__Host-bluelab_session"\n'
    'OPS_SESSION_COOKIE = "__Host-bluelab_ops_session"\n'
    'SAME_SITE = "strict"\n'
    'COOKIE_PATH = "/"\n'
    "def go(response, spec, value):\n"
    "    response.set_cookie(key=spec.name, value=value, httponly=True,\n"
    '                        secure=True, samesite="strict", path="/")\n'
)


def test_a_conforming_module_passes(cookie_world):
    cookie_world(module_source=CONFORMING)

    assert cookies.check_chokepoint() == []
    assert cookies.check_names() == []
    assert cookies.check_attributes() == []


def test_a_cookie_set_outside_the_chokepoint_is_flagged(cookie_world):
    """The failure this exists to catch, and an easy one to write — FastAPI puts
    `set_cookie` on every Response object."""
    cookie_world(
        module_source=CONFORMING,
        other_source='def route(response):\n    response.set_cookie(key="sneaky", value="x")\n',
    )

    assert kinds(cookies.check_chokepoint()) == {"cookie-bypass"}


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ('samesite="strict"', "cookie-samesite"),
        ("httponly=True,", "cookie-httponly"),
        ('path="/"', "cookie-path"),
    ],
)
def test_a_dropped_attribute_is_flagged(cookie_world, mutation, expected):
    replacement = 'samesite="lax"' if expected == "cookie-samesite" else ""
    cookie_world(module_source=CONFORMING.replace(mutation, replacement))

    assert expected in kinds(cookies.check_attributes())


def test_a_domain_attribute_is_flagged(cookie_world):
    """The attribute the __Host- prefix forbids, and the one that would let a
    hostile subdomain set a cookie this application accepts."""
    cookie_world(module_source=CONFORMING.replace('path="/")', 'path="/", domain=".example.com")'))

    assert "cookie-domain" in kinds(cookies.check_attributes())


def test_a_name_without_the_host_prefix_is_flagged(cookie_world):
    cookie_world(module_source=CONFORMING.replace('"__Host-bluelab_session"', '"bluelab_session"'))

    assert kinds(cookies.check_names()) == {"cookie-prefix"}


@pytest.mark.parametrize(
    ("constant", "bad"),
    [("SAME_SITE", '"lax"'), ("COOKIE_PATH", '"/api"')],
)
def test_a_weakened_policy_constant_is_flagged(cookie_world, constant, bad):
    """The constants the emitting call is *supposed* to be built from.

    `ATTRIBUTE_RULES` checks what the call actually passes, so a module could
    weaken `SAME_SITE` to "lax" and still emit a literal "strict" — the two agree
    today and nothing was asserting they keep agreeing. Found by mutating
    `_check_literal` and watching nothing fail.
    """
    original = {"SAME_SITE": '"strict"', "COOKIE_PATH": '"/"'}[constant]
    cookie_world(module_source=CONFORMING.replace(f"{constant} = {original}", f"{constant} = {bad}"))

    violations = cookies.check_names()

    assert kinds(violations) == {"cookie-constant"}
    assert constant in violations[0].detail


def test_a_module_that_sets_no_cookie_at_all_is_flagged(cookie_world):
    """An audit that found nothing to audit must not report success — the helper
    was renamed, or the module stopped emitting cookies."""
    cookie_world(module_source='SESSION_COOKIE = "__Host-a"\nOPS_SESSION_COOKIE = "__Host-b"\n')

    assert kinds(cookies.check_attributes()) == {"cookie-none"}


@pytest.mark.verifies("SEC-002")
def test_the_committed_cookie_module_conforms():
    """SEC-002 on the source as committed: the `__Host-` prefix on every name, and
    `HttpOnly`/`Secure`/`SameSite=Strict`/`Path=/` with no `Domain` at the one place
    allowed to emit a cookie."""
    assert cookies.check_chokepoint() == []
    assert cookies.check_names() == []
    assert cookies.check_attributes() == []


# ── mutation testing: the band, and the gate on it ────────────────────────────
#
# mutmut refuses to run on native Windows, so none of this executes a mutation
# run. What it does verify is everything that can be wrong *without* running one:
# which files the band selects (through mutmut's own config loader, not a
# reimplementation of it) and how the gate reads the tally mutmut leaves behind.


def test_the_mutation_band_is_exactly_the_invariant_paths():
    """quality/01 §4 scopes mutation testing to the invariant paths' application
    layer. Verified through mutmut's own `Config`, so this tracks the real matcher
    rather than a second copy of the glob logic that could agree with the patterns
    while mutmut disagrees with both.
    """
    from mutmut.configuration import Config

    config = Config.get()

    for path in (
        "src/bluelab/calls/admission.py",           # T-1
        "src/bluelab/calls/completion.py",          # T-2
        "src/bluelab/modules/review/grading.py",    # T-3, grading arithmetic
        "src/bluelab/modules/knowledge/publish.py",  # T-4
        "src/bluelab/modules/drills/freeze.py",     # T-5
        "src/bluelab/calls/interruption.py",        # T-6
        "src/bluelab/modules/hiring/invites.py",    # T-7
        "src/bluelab/modules/hiring/shortlist.py",  # T-8, T-9
        "src/bluelab/platform/db/scope.py",
        "src/bluelab/platform/db/privileged.py",
        "src/bluelab/platform/errors/denial.py",
    ):
        assert config.should_mutate(path), f"{path} is in the invariant band"

    for path in (
        "src/bluelab/modules/review/models.py",     # ORM declarations
        "src/bluelab/modules/drills/policies.py",   # feeds SQL, not application logic
        "src/bluelab/platform/config.py",           # settings
        "src/bluelab/platform/http/pagination.py",  # product-logic band, 85% bar
        "src/bluelab/api/v1.py",
    ):
        assert not config.should_mutate(path), f"{path} is outside the band"


def test_every_banded_file_exists():
    """A pattern that matches nothing removes a file from the gate in total
    silence — the run simply produces fewer mutants and still passes."""
    import tomllib

    config = tomllib.loads((TOOLS.parent / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["mutmut"]["only_mutate"]

    assert patterns, "the band must not be empty"
    assert [p for p in patterns if not (TOOLS.parent / p).exists()] == []


@pytest.fixture
def stats(tmp_path, monkeypatch):
    """Write a mutmut CI/CD stats file and point the gate at a scratch baseline."""
    monkeypatch.setattr(mutation, "BASELINE", tmp_path / "baseline.json")

    def _write(**overrides: object) -> Path:
        payload = {
            "killed": 90, "survived": 10, "total": 100, "no_tests": 0,
            "skipped": 0, "suspicious": 0, "timeout": 0, "segfault": 0,
            "check_was_interrupted_by_user": False,
        }
        payload.update(overrides)
        target = tmp_path / "mutmut-cicd-stats.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    return _write


def run_gate(path: Path, *args: str) -> int:
    import sys as _sys

    argv = ["check_mutation_score.py", "--stats", str(path), *args]
    original, _sys.argv = _sys.argv, argv
    try:
        return mutation.main()
    finally:
        _sys.argv = original


def test_mutants_with_no_test_count_as_survivors(stats):
    """`no_tests` is the most damning survivor class: the mutant was never given a
    chance to be caught. For "would we notice if this were wrong", uncovered and
    uncaught are the same answer."""
    tally = mutation.load_tally(stats(killed=80, survived=10, no_tests=10))

    assert tally.unkilled == 20
    assert tally.score == pytest.approx(0.8)


def test_zero_mutants_is_an_error_not_a_pass(stats):
    """`only_mutate` matching nothing would otherwise pass the gate having tested
    nothing at all — the emptiest false green available."""
    assert run_gate(stats(total=0, killed=0, survived=0)) == 2


def test_mutants_generated_but_never_judged_is_an_error(stats):
    """The shape a real aborted run produces, and the one that nearly slipped
    through: `total: 594, killed: 0, survived: 0`.

    A collection error in the selected suite stops mutmut before it executes
    anything, and it writes the stats file regardless. Without this guard the gate
    reads zero survivors, compares favourably against any baseline, and passes a
    run that tested nothing.
    """
    assert run_gate(stats(total=594, killed=0, survived=0, no_tests=0)) == 2


def test_an_interrupted_run_is_an_error(stats):
    """A partial tally makes every unreached mutant look killed."""
    assert run_gate(stats(check_was_interrupted_by_user=True)) == 2


def test_a_missing_stats_file_is_exit_two(tmp_path, monkeypatch):
    monkeypatch.setattr(mutation, "BASELINE", tmp_path / "baseline.json")

    assert run_gate(tmp_path / "absent.json") == 2


def test_a_malformed_stats_file_is_exit_two(tmp_path, monkeypatch):
    monkeypatch.setattr(mutation, "BASELINE", tmp_path / "baseline.json")
    broken = tmp_path / "broken.json"
    broken.write_text('{"killed": 1}', encoding="utf-8")

    assert run_gate(broken) == 2


@pytest.mark.parametrize("status", ["timeout", "suspicious", "segfault", "skipped"])
def test_a_mutant_that_never_returned_a_verdict_counts_as_unkilled(stats, status):
    """The cheapest way to raise a mutation score is to stop a mutant producing a
    verdict at all, and mutmut has four statuses for exactly that.

    Counting only `survived` and `no_tests` let those mutants leave the arithmetic:
    the same kills out of the same mutants read higher simply because fewer were
    judged. `--max-children 1` is mandatory here (the L3 suite shares one database),
    so a loaded runner parks marginal mutants in `timeout` with no help from anyone
    — and the gate used to report the loss as "mutants newly killed".
    """
    tally = mutation.load_tally(stats(killed=90, survived=0, no_tests=0, **{status: 10}))

    assert tally.unkilled == 10
    assert tally.score == pytest.approx(0.9)


def test_parking_mutants_cannot_move_the_score(stats):
    """The same 90 kills out of the same 100 mutants, judged two different ways."""
    judged = mutation.load_tally(stats(killed=90, survived=10))
    parked = mutation.load_tally(
        stats(killed=90, survived=1, no_tests=1, timeout=5, suspicious=2, segfault=1)
    )

    assert judged.unkilled == parked.unkilled == 10
    assert judged.score == parked.score


def test_a_tally_that_does_not_account_for_every_mutant_is_exit_two(stats):
    """The statuses must sum to the total. When they do not, either this is not a
    mutmut file or mutmut has grown a status this gate does not count — and an
    uncounted status is a bucket mutants sit in while the gate ignores them."""
    assert run_gate(stats(killed=90, survived=5, total=100)) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("survived", -1000),   # subtracts real survivors from the comparison
        ("killed", -5),
        ("no_tests", -60),     # drove the score to 111.1%
        ("survived", 50.9),    # silently truncated to 50
        ("survived", True),    # bool subclasses int, so this used to read as 1
    ],
    ids=["negative-survived", "negative-killed", "negative-no-tests", "fraction", "bool"],
)
def test_a_count_that_cannot_come_from_mutmut_is_exit_two(stats, field, value):
    """`int()` accepts all of these. A mutant tally cannot contain any of them, so
    each means the file is not what it claims to be — could not run, not green."""
    assert run_gate(stats(**{field: value})) == 2


def test_no_baseline_refuses_to_forgive_the_backlog(stats):
    assert run_gate(stats()) == 1


@pytest.mark.parametrize(
    "content",
    ['{"unkilled": 60', '{"ceiling": 60}', '{"unkilled": "sixty"}', '{"unkilled": null}'],
    ids=["malformed", "missing-key", "non-numeric", "null"],
)
def test_a_damaged_baseline_is_exit_two_not_one(stats, tmp_path, content):
    """Exit 1 says "the gate is red" and sends the reader hunting for surviving
    mutants; this is "the check could not run". It also used to arrive as a raw
    traceback, because `load_baseline` sat outside the try block that `load_tally`
    was inside."""
    mutation.BASELINE.write_text(content, encoding="utf-8")

    assert run_gate(stats()) == 2


def test_more_survivors_than_the_baseline_is_red(stats):
    run_gate(stats(survived=10), "--update-baseline")

    assert run_gate(stats(killed=88, survived=12)) == 1


def test_the_same_survivor_count_passes(stats):
    run_gate(stats(survived=10), "--update-baseline")

    assert run_gate(stats(survived=10)) == 0


def test_killing_a_mutant_forces_the_baseline_down(stats):
    """The half that makes it a ratchet: a slack ceiling would silently re-permit
    every mutant it still allows."""
    run_gate(stats(survived=10), "--update-baseline")

    assert run_gate(stats(killed=95, survived=5)) == 1
    assert run_gate(stats(killed=95, survived=5), "--update-baseline") == 0
    assert run_gate(stats(killed=95, survived=5)) == 0


# ── exit codes: the gate's actual interface with CI ───────────────────────────
#
# CI reads one number. Every branch that produces it earns a test, because a tool
# that finds the defect and then returns 0 has found nothing as far as the build is
# concerned — and that failure is invisible, because the build stays green.


@pytest.mark.parametrize(
    "tool",
    [conformance, coverage, tiers, cookies],
    ids=["conformance", "coverage", "tiers", "cookies"],
)
def test_a_clean_repository_exits_zero(tool, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["tool"])

    assert tool.main() == 0


def test_a_tier_branch_makes_the_scan_exit_one(tmp_path, monkeypatch):
    write(tmp_path / "m.py", "def f(s):\n    if s.rollout_tier:\n        return 1\n    return 2\n")
    monkeypatch.setattr(tiers, "BACKEND_SRC", tmp_path)
    monkeypatch.setattr(sys, "argv", ["scan_tier_branches.py"])

    assert tiers.main() == 1


def test_a_cookie_violation_makes_the_audit_exit_one(cookie_world, monkeypatch):
    cookie_world(
        module_source=CONFORMING,
        other_source='def route(response):\n    response.set_cookie(key="sneaky", value="x")\n',
    )
    monkeypatch.setattr(sys, "argv", ["audit_cookie_attributes.py"])

    assert cookies.main() == 1


# The two tools report contradictions in different shapes — `Finding` objects for
# the conformance diff, plain strings for coverage — so the stand-ins must match or
# the test exercises a code path that cannot exist.
CONTRADICTIONS = [
    (conformance, conformance.Finding("probe", "subject", "detail")),
    (coverage, "a contradiction"),
]
CONTRADICTION_IDS = ["conformance", "coverage"]


@pytest.mark.parametrize(("tool", "contradiction"), CONTRADICTIONS, ids=CONTRADICTION_IDS)
def test_a_contradiction_exits_one_even_with_an_empty_gap_set(tool, contradiction, monkeypatch):
    """Contradictions bypass the ratchet entirely — no baseline forgives them."""
    monkeypatch.setattr(tool, "run", lambda: ([contradiction], set(), []))
    monkeypatch.setattr(sys, "argv", ["tool"])

    assert tool.main() == 1


@pytest.mark.parametrize(("tool", "contradiction"), CONTRADICTIONS, ids=CONTRADICTION_IDS)
def test_baselining_refuses_to_bake_in_a_contradiction(tool, contradiction, monkeypatch, tmp_path):
    """`--update-baseline` forgives absence, never disagreement. Recording one would
    convert live drift into an accepted fact of the repository."""
    monkeypatch.setattr(ratchet, "BASELINE_DIR", tmp_path)
    monkeypatch.setattr(tool, "run", lambda: ([contradiction], set(), []))
    monkeypatch.setattr(sys, "argv", ["tool", "--update-baseline"])

    assert tool.main() == 1
    assert list(tmp_path.iterdir()) == [], "nothing may be written while drift stands"


# ── local database bootstrap ─────────────────────────────────────────────────


def _example_environment() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in (BACKEND_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def test_local_compose_bootstraps_constrained_database_roles():
    """The image-created POSTGRES_USER is a superuser, so it must be a distinct
    bootstrap identity rather than the migration or application identity."""
    compose = yaml.safe_load((BACKEND_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    postgres = compose["services"]["postgres"]

    assert postgres["environment"]["POSTGRES_USER"] == "postgres"
    assert any(
        volume.startswith("./sql/roles/bootstrap.sql:/docker-entrypoint-initdb.d/")
        for volume in postgres["volumes"]
    )

    bootstrap = (BACKEND_ROOT / "sql/roles/bootstrap.sql").read_text(encoding="utf-8").lower()
    assert "nosuperuser bypassrls createrole" in bootstrap
    assert "nosuperuser nobypassrls nocreaterole" in bootstrap
    assert "revoke all on schema public from public" in bootstrap
    assert "alter default privileges for role" in bootstrap


def test_local_runtime_and_migrations_use_different_database_roles():
    environment = _example_environment()

    assert urlsplit(environment["DATABASE_URL"]).username == "bluelab_app"
    assert urlsplit(environment["MIGRATION_DATABASE_URL"]).username == "bluelab"
