"""Launch the official grader with Linux artifact bytes on Windows hosts."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path, WindowsPath


def write_linux_artifact(path: Path, data: str, encoding: str | None = None, errors: str | None = None) -> int:
    """Avoid Windows text-mode CRLF translation for files copied into Linux."""
    return path.write_bytes(data.replace("\r\n", "\n").encode(encoding or "utf-8", errors or "strict"))


class _ArtifactWindowsPath(WindowsPath):
    def write_text(self, data: str, encoding: str | None = None, errors: str | None = None,
                   newline: str | None = None) -> int:
        if self.name in {"eval.sh", "patch.diff"}:
            return write_linux_artifact(self, data, encoding, errors)
        return super().write_text(data, encoding=encoding, errors=errors, newline=newline)


def grade(dataset: str, split: str, instances: list[str], predictions: str,
          run_id: str, report_dir: str) -> None:
    """Scoped compatibility adapter for swebench 5.0.2, inside a child process.

    Only the grader module's Path constructor changes. No global pathlib or
    installed dependency files are patched; test execution/scoring is official.
    """
    grader = importlib.import_module("swebench.harness.run_evaluation")
    original = getattr(grader, "Path")
    try:
        if sys.platform == "win32":
            setattr(grader, "Path", _ArtifactWindowsPath)
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        grader.main(dataset_name=dataset, split=split, instance_ids=instances,
                    predictions_path=predictions, max_workers=1, open_file_limit=4096,
                    run_id=run_id, timeout=1800, rewrite_reports=False, modal=False,
                    report_dir=report_dir)
    finally:
        setattr(grader, "Path", original)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset_name", required=True)
    parser.add_argument("--split", default="dev")
    parser.add_argument("--instance_ids", nargs="+", required=True)
    parser.add_argument("--predictions_path", required=True)
    parser.add_argument("--max_workers", type=int, choices=(1,), default=1)
    parser.add_argument("--run_id", required=True)
    parser.add_argument("--report_dir", default=".tmp/swebench-reports")
    args = parser.parse_args()
    grade(args.dataset_name, args.split, args.instance_ids, args.predictions_path,
          args.run_id, args.report_dir)


if __name__ == "__main__":
    main()
