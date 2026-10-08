"""Run the unittest suite; in GitHub Actions, also report each failure as an annotation.

Job logs require repository admin rights, but annotations are public, so the failing
test and the end of its traceback stay visible to anyone looking at the run.
"""

from __future__ import annotations

import os
import sys
import unittest


def annotate(kind: str, test: unittest.TestCase, trace: str) -> None:
    tail = "\n".join(trace.strip().splitlines()[-25:])
    text = tail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title={kind} {test.id()}::{text}")


def main() -> int:
    suite = unittest.defaultTestLoader.discover("tests", pattern="test_*.py", top_level_dir="tests")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for test, trace in result.failures:
            annotate("FAIL", test, trace)
        for test, trace in result.errors:
            annotate("ERROR", test, trace)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
    sys.exit(main())
