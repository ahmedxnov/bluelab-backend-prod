"""Per-attempt cost accounting against NFR-006, assembled from vendor usage fields,
plus the modeled-versus-billed divergence guard (infra C-4). The detector ships
with the code it watches, from the first tier (quality/08 §1 item 6).

## What NFR-006 actually requires

≤ USD 1.00 **at p95** per completed attempt, all external calls included. The
stack's own roll-up puts a typical 300-second call at ≈ $0.38–0.43 and the
900-second maximum **at** the ceiling on list prices (stack/00 §4). So the
headroom is real but the worst case is not comfortable, and the figure has to be
measured rather than assumed.

Cost is accumulated **per attempt**, across planes: the call plane's STT, TTS,
conversational model, and transport spend, plus this plane's grading and report
spend. `attempt_id` is the join key that makes that possible — which is exactly
why it is on the correlation spine (observability/01 §3).

## Why "modeled versus billed" is a separate signal

`attempt.cost_usd` is *modeled*: unit prices multiplied by vendor-reported usage.
The vendor's invoice is the truth. They diverge when a price changes, when a
plan tier shifts, or when a usage field means something other than we assumed —
and every one of those is silent. The divergence guard compares the modeled
month-to-date total against `vendor.spend_fraction`'s billed figure and alarms on
the gap, so a wrong model surfaces as a discrepancy rather than as a surprise
invoice (infra C-4).

At Tier 1 that matters more than at any later rung: the $120/month hard cap is
held by capping volume, and the per-vendor caps with their 80 % warnings are what
enforce it (specs/01 §3.5). A cost model that drifts low would let the volume cap
be set too high.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from opentelemetry import metrics

from bluelab.platform.telemetry import correlation

_meter: Final = metrics.get_meter("bluelab.cost")

attempt_cost_usd = _meter.create_histogram(
    "attempt.cost_usd",
    unit="USD",
    description="Modeled per-attempt cost, all external calls included — NFR-006",
)
vendor_spend_fraction = _meter.create_gauge(
    "vendor.spend_fraction",
    description="Per-vendor month-to-date BILLED spend / cap — the 80% warning (SEC-019)",
)
cost_model_divergence = _meter.create_gauge(
    "cost.model_divergence",
    description="Modeled month-to-date / billed month-to-date — the C-4 guard",
)

NFR_006_CEILING_USD: Final = Decimal("1.00")
"""≤ USD 1.00 at p95 per completed attempt (specs/01). Invariant at every tier."""


class CostCapability(StrEnum):
    """The capabilities that spend money on an attempt.

    The turn-path four are billed by the call plane and reported across the seam;
    the rest are billed here. Both land on the same `attempt_id`.
    """

    STT = "stt"
    TTS = "tts"
    CONVERSATIONAL = "conversational"
    TRANSPORT = "transport"
    EVALUATOR = "evaluator"
    GENERATION = "generation"
    EXTRACTION = "extraction"
    RENDER = "render"


@dataclass(frozen=True, slots=True)
class CostEntry:
    """One capability's spend on one attempt, with the usage it was derived from.

    `usage` is kept because it is what makes a divergence diagnosable: knowing
    the modeled total is wrong is useless without knowing which term moved.
    Units are the vendor's own (characters, tokens, seconds) and are content-free
    — a token *count* is not content (observability/01 §5).
    """

    capability: CostCapability
    usd: Decimal
    usage_unit: str
    usage_quantity: Decimal
    model_version: str | None = None
    """The vendor-served model or alias id, so a silent alias move is visible
    against a cost change (observability/01 §4.3, R-16)."""


class AttemptCost:
    """Accumulates one attempt's spend across capabilities and planes."""

    def __init__(self, attempt_id: UUID) -> None:
        self.attempt_id = attempt_id
        self._entries: list[CostEntry] = []

    def add(self, entry: CostEntry) -> None:
        self._entries.append(entry)

    @property
    def entries(self) -> tuple[CostEntry, ...]:
        return tuple(self._entries)

    @property
    def total_usd(self) -> Decimal:
        return sum((entry.usd for entry in self._entries), start=Decimal(0))

    @property
    def exceeds_ceiling(self) -> bool:
        """True if this single attempt is over the NFR-006 figure.

        One attempt over the ceiling is not a breach — the requirement binds p95,
        and the 900-second maximum sits at the ceiling by design. It is a signal
        worth counting, not an error worth raising.
        """
        return self.total_usd > NFR_006_CEILING_USD

    def record(self) -> None:
        """Emit the attempt's modeled cost.

        One sample per attempt, so the histogram's p95 *is* the NFR-006
        measurement rather than a proxy for it.
        """
        # Bound once. Written as `correlation.current().metric_labels() if
        # correlation.current() else {}` this called `current()` TWICE, so a
        # context that expired between the two evaluations would pass the guard
        # and then raise on the second call — the exact race the guard was
        # written to prevent.
        active = correlation.current()
        labels = active.metric_labels() if active else {}
        attempt_cost_usd.record(float(self.total_usd), labels)


def record_vendor_spend(*, vendor: str, billed_usd: Decimal, cap_usd: Decimal) -> float:
    """Publish a vendor's month-to-date **billed** spend against its cap.

    The 80 % warning is mandatory and is a Pre-T1 checklist item: hard caps live
    on every vendor key, sized to the ≈200 attempts/month ceiling
    (specs/01 §3.5, LADDER Pre-T1).

    Returns:
        The fraction, so the caller can act on it as well as publish it.
    """
    fraction = float(billed_usd / cap_usd) if cap_usd else 0.0
    vendor_spend_fraction.set(fraction, {"vendor": vendor})
    return fraction


def record_model_divergence(*, modeled_usd: Decimal, billed_usd: Decimal) -> float:
    """Publish modeled-versus-billed for the month — the C-4 guard.

    A ratio far from 1.0 means the cost model is wrong, which is worth knowing
    *before* the invoice rather than from it.

    Returns:
        `modeled / billed`, or 0.0 before any billed spend exists.
    """
    ratio = float(modeled_usd / billed_usd) if billed_usd else 0.0
    cost_model_divergence.set(ratio, {})
    return ratio
