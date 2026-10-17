"""Strict JSON reading and atomic replacement shared by both services."""

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from valutatrade_hub.core.exceptions import StorageError


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Overflowing JSON number")
    return number


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    """Read UTF-8 JSON without accepting NaN, infinity, or duplicate keys."""
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(
                stream,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
                object_pairs_hook=_unique_object,
            )
    except (OSError, ValueError, UnicodeError) as error:
        raise StorageError(f"Не удалось прочитать JSON-файл '{path.name}'") from error


def atomic_write_json(path: Path, data: Any) -> None:
    """Close and fsync a unique sibling temporary file before os.replace."""
    temporary_path = None
    try:
        contents = (
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except (OSError, TypeError, ValueError, OverflowError) as error:
        raise StorageError(f"Не удалось сохранить JSON-файл '{path.name}'") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
