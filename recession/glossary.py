"""Back-compat shim — explanations now live in registry.yaml (single source of
truth). Kept so any external import of glossary keeps working."""
from __future__ import annotations

from recession.registry import metric_info, section_info  # noqa: F401
