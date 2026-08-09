"""Analytics layer — pure, I/O-free functions over a normalized `Book`."""
from __future__ import annotations

from .engine import (
    Bucket,
    Buckets,
    CoveredCallViolation,
    MarginSnapshot,
    Posture,
    VxnPosture,
    WaterfallResult,
    WaterfallStep,
    assignment_waterfall,
    buckets,
    covered_calls_below_basis,
    get_vix,
    get_vxn,
    margin_snapshot,
    positions_breakdown,
    sector_analysis,
    summary,
    vix_posture,
    vxn_posture,
)
from .covered_calls import (
    DELTA_TARGETS,
    recommend_covered_calls,
    recommend_covered_calls_multi,
)

__all__ = [
    "Bucket", "Buckets", "WaterfallStep", "WaterfallResult", "MarginSnapshot",
    "Posture", "VxnPosture", "CoveredCallViolation",
    "buckets", "assignment_waterfall", "margin_snapshot", "vxn_posture",
    "vix_posture", "get_vxn", "get_vix", "covered_calls_below_basis",
    "summary", "positions_breakdown", "sector_analysis",
    "recommend_covered_calls", "recommend_covered_calls_multi", "DELTA_TARGETS",
]
