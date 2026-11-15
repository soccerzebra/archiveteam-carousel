"""Unit tests for the catalog wget-at compatibility check's pure logic (no docker/network)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

from check_catalog_compat import at_date, classify, required_min

OURS = {"has_wget": True, "version": "GNU Wget 1.21.3-at.20261026.01", "help_opts": ["--a", "--b"]}


def test_at_date_parsing():
    assert at_date("GNU Wget 1.21.3-at.20261026.01") == (20261026, 1)
    assert at_date("at.20260319.05") == (20260319, 5)
    assert at_date("no date here") is None


def test_required_min_takes_newest():
    assert required_min(["at.20240101.01", "at.20260319.02"]) == (20260319, 2)
    assert required_min([]) is None


def test_classify_scraper_is_na():
    status, _ = classify(OURS, {"has_wget": False})
    assert status == "N/A"


def test_classify_pass_when_subset_and_recent():
    proj = {"has_wget": True, "version": "GNU Wget 1.21.3-at.20260319.01",
            "help_opts": ["--a", "--b"], "used_opts": ["--a"], "req_versions": ["at.20260319.01"]}
    assert classify(OURS, proj)[0] == "PASS"


def test_classify_fail_on_old_shared_build():
    proj = {"has_wget": True, "version": "GNU Wget 1.21.3-at.20270101.01",
            "help_opts": ["--a"], "used_opts": ["--a"], "req_versions": ["at.20270101.01"]}
    status, reasons = classify(OURS, proj)
    assert status == "FAIL" and any("needs wget-at" in r for r in reasons)


def test_classify_fail_on_used_missing_flag():
    proj = {"has_wget": True, "version": "GNU Wget 1.21.3-at.20260319.01",
            "help_opts": ["--a", "--impersonate"], "used_opts": ["--impersonate"],
            "req_versions": []}
    status, reasons = classify(OURS, proj)
    assert status == "FAIL" and any("--impersonate" in r for r in reasons)


def test_classify_note_when_extra_flags_unused():
    # project's build has --x that ours lacks, but the pipeline never references it
    proj = {"has_wget": True, "version": "GNU Wget 1.21.3-at.20260319.01",
            "help_opts": ["--a", "--x"], "used_opts": ["--a"], "req_versions": []}
    assert classify(OURS, proj)[0] == "PASS-NOTE"
