"""S9 — registry is pure data. Adding a metric is a YAML edit, not a code edit."""
from __future__ import annotations

import textwrap

import yaml

from recession import registry


def test_registry_loads_from_yaml():
    assert len(registry.CORE_25) >= 25
    assert registry.by_id("UNRATE").section == "labor"
    # every series carries an explanation + a known section
    for s in registry.CORE_25:
        assert s.explain, f"{s.id} missing explain"
        assert s.section in registry.SECTIONS, f"{s.id} in unknown section {s.section}"


def test_section_and_metric_info():
    assert registry.section_info("labor")["label"] == "Labor"
    assert "Sahm" in registry.metric_info("SAHMREALTIME")


def test_adding_a_metric_is_data_only(tmp_path, monkeypatch):
    # simulate appending one YAML block → it flows through to a Series, no code change
    doc = yaml.safe_load(registry._YAML.read_text())
    base = len(doc["series"])
    doc["series"].append({"id": "TESTX", "source": "fred", "section": "labor",
                          "subsector": "levels", "name": "Test metric", "transform": "level",
                          "direction": "higher_is_worse", "weight": 1.0, "explain": "a test"})
    p = tmp_path / "registry.yaml"
    p.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(registry, "_YAML", p)
    sections, series = registry._load()
    ids = [s.id for s in series]
    assert "TESTX" in ids and len(series) == base + 1
    assert next(s for s in series if s.id == "TESTX").explain == "a test"
