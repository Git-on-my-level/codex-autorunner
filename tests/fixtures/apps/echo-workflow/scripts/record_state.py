import json
import os
import sys
from pathlib import Path


def main() -> None:
    state_dir = Path(os.environ["CAR_APP_STATE_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)

    message = " ".join(sys.argv[1:]) if sys.argv[1:] else "hello"
    repeat = int(os.environ.get("ECHO_REPEAT", "1"))

    record = {
        "message": message,
        "repeat": repeat,
        "app_id": os.environ.get("CAR_APP_ID", ""),
        "app_version": os.environ.get("CAR_APP_VERSION", ""),
        "hook_point": os.environ.get("CAR_HOOK_POINT"),
        "flow_run_id": os.environ.get("CAR_FLOW_RUN_ID"),
        "ticket_id": os.environ.get("CAR_TICKET_ID"),
    }

    state_file = state_dir / "records.jsonl"
    with state_file.open("a", encoding="utf-8") as fh:
        for _ in range(repeat):
            fh.write(json.dumps(record) + "\n")

    print(json.dumps({"ok": True, "message": message, "repeat": repeat}))


if __name__ == "__main__":
    main()
