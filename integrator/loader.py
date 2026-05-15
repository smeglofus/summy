import json
from pathlib import Path


class ErpDataLoadError(Exception):
    """Raised when the ERP feed cannot be loaded as a JSON array."""

    def __init__(self, message: str, retryable: bool = False):
        self.retryable = retryable
        super().__init__(message)


def load_erp_data(path: Path) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as exc:
        raise ErpDataLoadError(
            f"Cannot read ERP data from {path}: {exc}",
            retryable=True,
        ) from exc
    except json.JSONDecodeError as exc:
        raise ErpDataLoadError(f"Invalid JSON in ERP data file {path}: {exc}") from exc

    if not isinstance(data, list):
        raise ErpDataLoadError(f"Expected a JSON array at {path}, got {type(data).__name__}")
    return data
