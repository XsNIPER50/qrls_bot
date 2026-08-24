"""Helpers for safely persisting bot-owned runtime data."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_json_dump(path: str | os.PathLike[str], data: Any, **kwargs: Any) -> None:
    """Write JSON beside its destination, then atomically replace the old file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(data, temporary, **kwargs)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())

        os.replace(temporary_name, destination)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
