import json
from pathlib import Path


def load_erp_data(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array at {path}, got {type(data).__name__}")
    return data
