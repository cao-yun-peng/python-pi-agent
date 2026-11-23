"""Create separate grading/task inputs that share one local immutable image."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


INSTANCE = "pydicom__pydicom-1139"
IMAGE = "pi-agent/swebench-pydicom-1139:pytest-7.4.4-v1"
BASE = "sha256:a2344b4b6db428913523e14f576fe358d6ae0c349cd77ac82e616188955ddb3b"


def prepare(source: Path, output: Path) -> None:
    manifest = json.loads((source / "tasks.json").read_text(encoding="utf-8"))
    rows = json.loads((source / "grader-data.json").read_text(encoding="utf-8"))
    tasks = [row for row in manifest["tasks"] if row["instance_id"] == INSTANCE]
    grading = [row for row in rows if row["instance_id"] == INSTANCE]
    if len(tasks) != 1 or len(grading) != 1:
        raise ValueError("expected exactly one pydicom-1139 task and grading row")
    task, row = tasks[0], grading[0]
    if any(task[key] != row[key] for key in task):
        raise ValueError("task and grader inputs disagree")
    image = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE], text=True,
    ).strip()
    if not image.startswith("sha256:") or len(image) != 71:
        raise ValueError("expected an immutable local image ID")
    output.mkdir(parents=True, exist_ok=False)
    records = {
        "tasks.json": {"source": {**manifest["source"], "environment_profile": "pydicom-1139-pytest7-v1"},
                       "tasks": [{**task, "image": image}]},
        "grader-data.json": [{**row, "image": image}],
        "environment.json": {
            "instance_id": INSTANCE, "profile": "pydicom-1139-pytest7-v1",
            "image_tag": IMAGE, "image_id": image, "original_image": task["image"],
            "base_image_id": BASE, "pytest": "7.4.4", "changed_row_fields": ["image"],
            "original_grader_data_sha256": hashlib.sha256((source / "grader-data.json").read_bytes()).hexdigest(),
            "dataset_source": manifest["source"],
        },
    }
    for name, value in records.items():
        (output / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.output)
