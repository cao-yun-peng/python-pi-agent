"""Fail if this task's legacy setup/teardown hooks are not executed."""

import importlib.metadata
from pathlib import Path
import subprocess
import sys
import tempfile


TEST = '''import pytest

class TestLegacyHooks:
    events = []

    def setup(self):
        self.ready = True
        self.events.append("setup")

    def teardown(self):
        self.events.append("teardown")

    def test_prepared(self):
        assert self.ready
        self.events.append("test")

@pytest.fixture(scope="module", autouse=True)
def check_teardown():
    yield
    assert TestLegacyHooks.events == ["setup", "test", "teardown"]
'''


def main() -> int:
    print("pytest=" + importlib.metadata.version("pytest"), flush=True)
    with tempfile.TemporaryDirectory(prefix="pi-pytest-compat-") as directory:
        target = Path(directory) / "test_legacy_hooks.py"
        target.write_text(TEST, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(target)], cwd=directory,
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
