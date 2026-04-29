import json
import os
from pathlib import Path


def main() -> None:
    state_dir = Path(os.environ["CAR_APP_STATE_DIR"])
    artifact_dir = Path(os.environ["CAR_APP_ARTIFACT_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    records = []
    state_file = state_dir / "records.jsonl"
    if state_file.exists():
        for line in state_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))

    lines = [
        "# Echo Workflow Summary",
        "",
        f"Total records: {len(records)}",
        "",
    ]
    for i, record in enumerate(records, 1):
        lines.append(f"{i}. `{record.get('message', '')}`")
    lines.append("")

    summary_path = artifact_dir / "summary.md"
    summary_path.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({"ok": True, "records": len(records)}))


if __name__ == "__main__":
    main()
