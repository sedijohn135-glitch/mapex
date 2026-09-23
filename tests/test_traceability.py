"""A4: every GEM section listed in docs/TRACEABILITY.md has real code and a real test, and no section is missing."""

import importlib
import re
from pathlib import Path

DOC = Path("docs/TRACEABILITY.md").read_text()
ROW = re.compile(r"^\| (GEM[12] · [^|]+?) \| [^|]+ \| `([^`]+)::([^`]+)` \| `([^`]+)::([^`]+)` \|$", re.M)
REQUIRED = [
    "GEM1 · LAYER 2", "GEM1 · STEP 0A", "GEM1 · STEP 0B", "GEM1 · STEP 0C", "GEM1 · STEP 0D", "GEM1 · STEP 0E",
    "GEM1 · STEP 1", "GEM1 · STEP 2A", "GEM1 · STEP 2B", "GEM1 · STEP 2C", "GEM1 · STEP 2D", "GEM1 · STEP 2E",
    "GEM1 · STEP 2F", "GEM1 · STEP 3", "GEM1 · STEP 4", "GEM1 · STEP 5", "GEM1 · STEP 6", "GEM1 · STEP 7",
    "GEM1 · STEP 8", "GEM1 · LAYER 4",
    "GEM2 · LAYER 1", "GEM2 · MODULE 5", "GEM2 · P1", "GEM2 · P2", "GEM2 · MODULE 6", "GEM2 · P3", "GEM2 · P4.1",
    "GEM2 · P4.2", "GEM2 · P4.3", "GEM2 · P4.4", "GEM2 · MODULE 7", "GEM2 · MODULE 8", "GEM2 · LAYER 4",
    "GEM2 · LAYER 5", "GEM2 · LAYER 6", "GEM2 · LAYER 7", "GEM2 · LAYER 8", "GEM2 · LAYER 9", "GEM2 · LAYER 10",
    "GEM2 · LAYER 11", "GEM2 · LAYER 12",
]


def test_rows_parse():
    assert len(ROW.findall(DOC)) >= 70


def test_every_code_reference_exists():
    for section, path, name, _tpath, _tname in ROW.findall(DOC):
        mod = importlib.import_module(path.removesuffix(".py").replace("/", "."))
        obj = mod
        for part in name.split("."):
            assert hasattr(obj, part), f"{section}: {path}::{name} missing"
            obj = getattr(obj, part)


def test_every_test_reference_exists():
    for section, _path, _name, tpath, tname in ROW.findall(DOC):
        src = Path(tpath).read_text()
        assert re.search(rf"^def {re.escape(tname)}\(", src, re.M), f"{section}: {tpath}::{tname} missing"


def test_no_gem_section_missing():
    listed = {s.strip() for s, *_ in ROW.findall(DOC)}
    for sec in REQUIRED:
        assert sec in listed, f"{sec} has no traceability row"
    # and the source documents really contain these sections
    g1 = Path("docs/source/GEM1.md").read_text()
    g2 = Path("docs/source/GEM2.md").read_text()
    for n in range(0, 9):
        assert f"STEP {n}" in g1
    for token in ("MODULE 5", "MODULE 6", "MODULE 7", "MODULE 8", "P4.1", "P4.4", "LAYER 12"):
        assert token in g2
