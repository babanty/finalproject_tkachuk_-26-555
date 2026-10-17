"""Strict JSON handling and failure-safe atomic replacement."""

import pytest

from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.infra import json_io


def test_utf8_round_trip_in_unicode_directory(tmp_path):
    """JSON is human-readable UTF-8 even with Cyrillic paths and values."""
    directory = tmp_path / "данные с пробелами"
    directory.mkdir()
    path = directory / "пользователи.json"
    json_io.atomic_write_json(path, {"имя": "Алиса", "balance": 1.25})
    assert json_io.read_json(path) == {"имя": "Алиса", "balance": 1.25}
    assert "Алиса" in path.read_text(encoding="utf-8")
    assert not list(directory.glob("*.tmp"))


@pytest.mark.parametrize(
    "contents",
    [
        b"{bad",
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"1e999",
        b"-1e999",
        b'{"meta":{"unexpected":1e999}}',
        b'{"balance":1,"balance":2}',
        b"\xff",
    ],
)
def test_corrupt_json_is_reported_without_changing_file(tmp_path, contents):
    """Malformed JSON never becomes an empty document or an implicit reset."""
    path = tmp_path / "users.json"
    path.write_bytes(contents)
    with pytest.raises(StorageError, match="прочитать"):
        json_io.read_json(path)
    assert path.read_bytes() == contents


@pytest.mark.parametrize(
    "data", [{"rate": float("nan")}, {"rate": float("inf")}, object()]
)
def test_unserializable_values_do_not_replace_existing_file(tmp_path, data):
    """Reject invalid outgoing values before committing any new file."""
    path = tmp_path / "rates.json"
    json_io.atomic_write_json(path, {"old": 1})
    before = path.read_bytes()
    with pytest.raises(StorageError, match="сохранить"):
        json_io.atomic_write_json(path, data)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_replace_preserves_original_and_only_cleans_own_temp(
    tmp_path, monkeypatch
):
    """An interrupted replacement must not remove another writer's temp file."""
    path = tmp_path / "users.json"
    json_io.atomic_write_json(path, ["old"])
    foreign = tmp_path / ".users.json.foreign.tmp"
    foreign.write_text("foreign", encoding="utf-8")

    def fail_replace(source, destination):
        raise PermissionError("simulated replace failure")

    monkeypatch.setattr(json_io.os, "replace", fail_replace)
    with pytest.raises(StorageError):
        json_io.atomic_write_json(path, ["new"])
    assert json_io.read_json(path) == ["old"]
    assert list(tmp_path.glob("*.tmp")) == [foreign]
    assert foreign.read_text(encoding="utf-8") == "foreign"


def test_failed_fsync_preserves_original(tmp_path, monkeypatch):
    """Durability errors occur before the replacement is committed."""
    path = tmp_path / "users.json"
    json_io.atomic_write_json(path, [])

    def fail_fsync(descriptor):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(json_io.os, "fsync", fail_fsync)
    with pytest.raises(StorageError):
        json_io.atomic_write_json(path, ["new"])
    assert json_io.read_json(path) == []
    assert not list(tmp_path.glob("*.tmp"))
