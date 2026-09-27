"""Append-only JSONL record of everything a run did."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, state_dir: Path, run_id: str) -> None:
        self.run_id = run_id
        self.path = state_dir / "runs" / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **data: Any) -> None:
        entry = {
            "ts": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "event": event,
            **data,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    @staticmethod
    def read(state_dir: Path, run_id: str) -> list[dict[str, Any]]:
        path = state_dir / "runs" / f"{run_id}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"no audit log for run {run_id!r} at {path}")
        with path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
