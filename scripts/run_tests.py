"""Run unittest and expose structured failure diagnostics in GitHub checks."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def escape_annotation(value: str, *, property_value: bool = False) -> str:
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        value = value.replace(":", "%3A").replace(",", "%2C")
    return value


class AnnotatedResult(unittest.TextTestResult):
    def annotate(self, test, detail: str) -> None:
        if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
            return
        title = escape_annotation(test.id()[:250], property_value=True)
        message = escape_annotation(detail[-12000:])
        print(f"\n::error title={title}::{message}", flush=True)

    def addError(self, test, err) -> None:
        super().addError(test, err)
        self.annotate(test, self.errors[-1][1])

    def addFailure(self, test, err) -> None:
        super().addFailure(test, err)
        self.annotate(test, self.failures[-1][1])

    def addSubTest(self, test, subtest, err) -> None:
        errors, failures = len(self.errors), len(self.failures)
        super().addSubTest(test, subtest, err)
        if len(self.errors) > errors:
            self.annotate(subtest, self.errors[-1][1])
        elif len(self.failures) > failures:
            self.annotate(subtest, self.failures[-1][1])


def main() -> int:
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2, resultclass=AnnotatedResult).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
