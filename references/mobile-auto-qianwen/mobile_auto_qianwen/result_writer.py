import json
from pathlib import Path
from typing import Any


def write_result(path: str | Path, data: dict, finished: bool = False) -> None:
    if finished:
        data = {**data, "finished": True}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def aggregate_results(result_paths: list[str | Path]) -> dict:
    sessions: list[dict] = []
    total = 0
    success = 0
    partial = 0
    failed = 0
    for p in result_paths:
        content = Path(p).read_text(encoding="utf-8")
        data = json.loads(content)
        sessions.append(data)
        for q in data.get("questions", []):
            total += 1
            status = q.get("status", "failed")
            if status == "success":
                success += 1
            elif status == "partial":
                partial += 1
            else:
                failed += 1
    return {
        "sessions": sessions,
        "summary": {"total": total, "success": success, "partial": partial, "failed": failed},
    }
