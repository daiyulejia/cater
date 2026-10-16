"""Run unittest and expose structured failure diagnostics in GitHub checks."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.test_progress import ProgressWatchdog

SLOWEST_LIMIT = 10


def shard_argument(value: str) -> tuple[int, int]:
    if len(value) > 7 or not re.fullmatch(r"[1-9][0-9]*/[1-9][0-9]*", value, re.ASCII):
        raise argparse.ArgumentTypeError("shard must be INDEX/TOTAL with 1 <= INDEX <= TOTAL <= 128")
    index, total = map(int, value.split("/"))
    if not 1 <= index <= total <= 128:
        raise argparse.ArgumentTypeError("shard must be INDEX/TOTAL with 1 <= INDEX <= TOTAL <= 128")
    return index, total


def module_shard(module: str, total: int) -> int:
    digest = hashlib.sha256(module.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % total + 1


class UnsupportedShardSuiteError(ValueError):
    pass


def test_cases(suite, *, plain_suites: bool = False):
    if plain_suites and type(suite) is not unittest.TestSuite:
        raise UnsupportedShardSuiteError
    for item in suite:
        if isinstance(item, unittest.BaseTestSuite):
            yield from test_cases(item, plain_suites=plain_suites)
        else:
            yield item


def select_tests(cases: list, shard: tuple[int, int] | None) -> list:
    if shard is None:
        return list(cases)
    index, total = shard
    return [case for case in cases if module_shard(type(case).__module__, total) == index]


def escape_annotation(value: str, *, property_value: bool = False) -> str:
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        value = value.replace(":", "%3A").replace(",", "%2C")
    return value


def progress_call(progress, method: str, *args) -> None:
    try:
        if progress is not None:
            getattr(progress, method)(*args)
    except Exception:
        # Optional diagnostics must never replace the suite's outcome.
        pass


class AnnotatedResult(unittest.TextTestResult):
    def __init__(self, *args, progress=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.progress = progress
        self.slowest: list[tuple[float, str]] = []
        self._started = 0.0

    def startTest(self, test) -> None:
        progress_call(self.progress, "start_case", test)
        super().startTest(test)
        self._started = time.perf_counter()

    def stopTest(self, test) -> None:
        elapsed = max(0.0, time.perf_counter() - self._started)
        entry = (elapsed, test.id()[:250])
        if len(self.slowest) < SLOWEST_LIMIT:
            heapq.heappush(self.slowest, entry)
        else:
            heapq.heappushpop(self.slowest, entry)
        super().stopTest(test)
        progress_call(self.progress, "stop_case")

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run all standard-library tests or a deterministic module shard.")
    parser.add_argument("--shard", type=shard_argument, metavar="INDEX/TOTAL",
                        help="One-based module shard, for example 1/2 or 2/2; omitted runs the full suite")
    parser.add_argument("--list-tests", action="store_true", help="List selected test IDs without executing tests")
    args = parser.parse_args([] if argv is None else argv)
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        try:
            reconfigure = getattr(sys.stdout, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

    progress = None
    try:
        if not args.list_tests and os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
            try:
                progress = ProgressWatchdog(sys.stdout, escape_annotation)
            except Exception:
                pass
        progress_call(progress, "start")
        return run_selected(args, progress)
    finally:
        progress_call(progress, "close")


def run_selected(args, progress) -> int:
    loader = unittest.defaultTestLoader
    loader.errors.clear()
    try:
        suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))
        cases = list(test_cases(suite, plain_suites=args.shard is not None))
    except UnsupportedShardSuiteError:
        print("Sharding requires a tree of plain unittest.TestSuite objects; custom suites and "
              "BaseTestSuite cannot be flattened safely. Run the full suite without --shard; "
              "no tests executed.", file=sys.stderr, flush=True)
        return 1
    except SystemExit:
        print("Test discovery failed (SystemExit); no tests executed.", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:
        print(f"Test discovery failed ({type(exc).__name__}); no tests executed.", file=sys.stderr, flush=True)
        return 1
    failed = [case for case in cases if isinstance(case, unittest.loader._FailedTest)]
    if loader.errors or failed:
        print(f"Test discovery failed: {len(loader.errors)} loader error(s), {len(failed)} failed test(s); "
              "no shard selection applied and no tests executed.", file=sys.stderr, flush=True)
        for case in failed[:SLOWEST_LIMIT]:
            print("  Failed discovery ID: " + json.dumps(case.id()[:250]), file=sys.stderr, flush=True)
        if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
            print("::error title=Test discovery::Full discovery failed before shard selection.", flush=True)
        return 1

    selected = select_tests(cases, args.shard)
    if not selected:
        print("Test selection is empty; refusing to report success.", file=sys.stderr, flush=True)
        return 1
    modules = {type(case).__module__ for case in cases}
    selected_modules = {type(case).__module__ for case in selected}
    label = f"shard {args.shard[0]}/{args.shard[1]}" if args.shard else "full suite"
    duplicates = len(cases) - len({case.id() for case in cases})
    print(f"Selection: {label}; {len(selected)}/{len(cases)} execution items; "
          f"{len(selected_modules)}/{len(modules)} modules; "
          f"{duplicates} existing repeated ID occurrence(s) in full discovery.",
          file=sys.stderr, flush=True)
    if args.list_tests:
        for name in sorted(case.id() for case in selected):
            print(name)
        return 0

    # Keep the discovered tree intact for full runs, including load_tests order.
    execution = unittest.TestSuite(selected) if args.shard else suite
    resultclass = AnnotatedResult
    if progress is not None:
        progress_call(progress, "select", len(selected))

        def resultclass(*args, **kwargs):
            return AnnotatedResult(*args, progress=progress, **kwargs)

    result = unittest.TextTestRunner(verbosity=2, resultclass=resultclass).run(execution)
    if result.slowest:
        print(f"Slowest tests (up to {SLOWEST_LIMIT}; includes per-test setup/cleanup, "
              "excludes discovery and module/class fixtures):", file=sys.stderr, flush=True)
        for elapsed, name in sorted(result.slowest, reverse=True):
            print(f"  {elapsed:.3f}s {json.dumps(name)}", file=sys.stderr, flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
