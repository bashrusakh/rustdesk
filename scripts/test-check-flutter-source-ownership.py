#!/usr/bin/env python3
"""Focused tests for active-root workflow checkout pin enforcement."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("check-flutter-source-ownership.py").resolve()
APPROVED_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


def load_scanner():
    spec = importlib.util.spec_from_file_location("ownership_scanner", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load scanner: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CheckoutPinTests(unittest.TestCase):
    def test_current_root_workflows_use_approved_sha(self) -> None:
        scanner = load_scanner()
        failures: list[str] = []
        scanner.check_workflow_checkout_pins(
            {"scan_policy": {"active_root_workflow_checkout_sha": APPROVED_SHA}},
            failures,
        )
        self.assertEqual(failures, [])
        self.assertTrue(scanner.WORKFLOWS)
        self.assertTrue(
            all(path.parent == scanner.ROOT / ".github" / "workflows" for path in scanner.WORKFLOWS)
        )

    def test_mismatch_and_tag_fail_but_copied_workflow_is_ignored(self) -> None:
        scanner = load_scanner()
        original_root = scanner.ROOT
        original_workflows = scanner.WORKFLOWS
        policy = {"scan_policy": {"active_root_workflow_checkout_sha": APPROVED_SHA}}
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                active = root / ".github" / "workflows" / "active.yml"
                inactive = root / "third_party" / "copied" / ".github" / "workflows" / "inactive.yml"
                active.parent.mkdir(parents=True)
                inactive.parent.mkdir(parents=True)
                inactive.write_text("uses: actions/checkout@v4\n", encoding="utf-8")
                scanner.ROOT = root
                scanner.WORKFLOWS = [active]

                active.write_text("uses: actions/checkout@" + "a" * 40 + "\n", encoding="utf-8")
                failures: list[str] = []
                scanner.check_workflow_checkout_pins(policy, failures)
                self.assertEqual(len(failures), 1)
                self.assertIn(APPROVED_SHA, failures[0])

                active.write_text("uses: actions/checkout@v7\n", encoding="utf-8")
                failures = []
                scanner.check_workflow_checkout_pins(policy, failures)
                self.assertEqual(len(failures), 1)
                self.assertIn("exact 40-character SHA", failures[0])
        finally:
            scanner.ROOT = original_root
            scanner.WORKFLOWS = original_workflows


if __name__ == "__main__":
    unittest.main()
