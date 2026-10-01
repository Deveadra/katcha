from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from katcha.config import get_settings
from katcha.intelligence_ingest_contract import IngestIntelligenceBatchRequest
from katcha.services.ingestion_sources import ingest_intelligence_batch

_MAX_HANDOFF_BYTES = 10 * 1024 * 1024
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}\.json$")


@dataclass(frozen=True, slots=True)
class HandoffInboxItem:
    filename: str
    status: str
    size_bytes: int
    modified_at: datetime
    channel_profile_id: str | None = None
    batch_key: str | None = None
    record_count: int | None = None
    error: str | None = None
    receipt: dict[str, Any] | None = None


def handoff_root(root: Path | None = None) -> Path:
    return Path(root or get_settings().intelligence_handoff_dir).expanduser().resolve()


def _paths(root: Path | None = None) -> dict[str, Path]:
    base = handoff_root(root)
    paths = {
        "root": base,
        "incoming": base / "incoming",
        "processed": base / "processed",
        "failed": base / "failed",
        "receipts": base / "receipts",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _safe_filename(filename: str) -> str:
    value = Path(str(filename or "")).name
    if value != filename or not _FILENAME.fullmatch(value):
        raise ValueError(
            "handoff filename must be a simple .json name using letters, numbers, "
            "dots, dashes, or underscores"
        )
    return value


def _is_regular_handoff_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _read_limited(path: Path) -> bytes:
    if not _is_regular_handoff_file(path):
        raise ValueError("handoff path must be a regular file")
    size = path.stat().st_size
    if size < 1:
        raise ValueError("handoff file is empty")
    if size > _MAX_HANDOFF_BYTES:
        raise ValueError("handoff file exceeds the 10 MiB limit")
    return path.read_bytes()


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _receipt_path(paths: dict[str, Path], filename: str) -> Path:
    return paths["receipts"] / f"{filename}.receipt.json"


def _load_receipt(paths: dict[str, Path], filename: str) -> dict[str, Any] | None:
    path = _receipt_path(paths, filename)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _file_item(path: Path, status: str, paths: dict[str, Path]) -> HandoffInboxItem:
    stat = path.stat()
    receipt = _load_receipt(paths, path.name)
    return HandoffInboxItem(
        filename=path.name,
        status=status,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        channel_profile_id=(
            str(receipt.get("channel_profile_id"))
            if receipt and receipt.get("channel_profile_id")
            else None
        ),
        batch_key=(
            str(receipt.get("batch_key"))
            if receipt and receipt.get("batch_key")
            else None
        ),
        record_count=(
            int(receipt.get("record_count"))
            if receipt and receipt.get("record_count") is not None
            else None
        ),
        error=(
            str(receipt.get("error"))
            if receipt and receipt.get("error")
            else None
        ),
        receipt=receipt,
    )


def list_handoff_inbox(
    *,
    root: Path | None = None,
    limit: int = 100,
) -> list[HandoffInboxItem]:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    paths = _paths(root)
    items: list[HandoffInboxItem] = []
    for status in ("incoming", "failed", "processed"):
        for path in paths[status].glob("*.json"):
            if _is_regular_handoff_file(path):
                items.append(_file_item(path, status, paths))
    items.sort(key=lambda item: (item.modified_at, item.filename), reverse=True)
    return items[:limit]


def submit_handoff_file(
    filename: str,
    content: bytes,
    *,
    root: Path | None = None,
) -> HandoffInboxItem:
    safe_name = _safe_filename(filename)
    if not content:
        raise ValueError("handoff file is empty")
    if len(content) > _MAX_HANDOFF_BYTES:
        raise ValueError("handoff file exceeds the 10 MiB limit")
    paths = _paths(root)
    for status in ("incoming", "processed", "failed"):
        existing = paths[status] / safe_name
        if not existing.exists():
            continue
        existing_bytes = _read_limited(existing)
        if hashlib.sha256(existing_bytes).digest() != hashlib.sha256(content).digest():
            raise ValueError(
                "a handoff file with this name already exists with different content"
            )
        return _file_item(existing, status, paths)
    target = paths["incoming"] / safe_name
    _write_atomic(target, content)
    return _file_item(target, "incoming", paths)


def _write_receipt(
    paths: dict[str, Path],
    filename: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    body = json.dumps(
        payload,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")
    _write_atomic(_receipt_path(paths, filename), body)
    return payload


def _failure_receipt(
    *,
    filename: str,
    error: Exception,
    parsed: IngestIntelligenceBatchRequest | None,
) -> dict[str, Any]:
    return {
        "filename": filename,
        "status": "failed",
        "channel_profile_id": (
            str(parsed.channel_profile_id) if parsed is not None else None
        ),
        "batch_key": parsed.batch_key if parsed is not None else None,
        "record_count": len(parsed.records) if parsed is not None else None,
        "error": str(error)[:4000],
        "processed_at": datetime.now(UTC).isoformat(),
    }


def process_handoff_file(
    filename: str,
    *,
    root: Path | None = None,
) -> HandoffInboxItem:
    safe_name = _safe_filename(filename)
    paths = _paths(root)
    source = paths["incoming"] / safe_name
    if not source.exists():
        for status in ("processed", "failed"):
            existing = paths[status] / safe_name
            if existing.exists():
                return _file_item(existing, status, paths)
        raise ValueError(f"handoff file not found: {safe_name}")

    parsed: IngestIntelligenceBatchRequest | None = None
    try:
        body = _read_limited(source)
        try:
            parsed = IngestIntelligenceBatchRequest.model_validate_json(body)
        except ValidationError as exc:
            raise ValueError(f"handoff JSON failed validation: {exc}") from exc

        result = ingest_intelligence_batch(
            channel_profile_id=parsed.channel_profile_id,
            batch_key=parsed.batch_key,
            producer=parsed.producer,
            source_type=parsed.source_type,
            batch_metadata=parsed.batch_metadata,
            records=[item.model_dump(mode="python") for item in parsed.records],
        )
        receipt = _write_receipt(
            paths,
            safe_name,
            {
                "filename": safe_name,
                "status": "processed",
                "channel_profile_id": str(result.batch.channel_profile_id),
                "batch_id": str(result.batch.id),
                "batch_key": result.batch.batch_key,
                "producer": result.batch.producer,
                "source_type": result.batch.source_type,
                "content_sha256": result.batch.content_sha256,
                "record_count": result.batch.record_count,
                "created_count": result.created_count,
                "updated_count": result.updated_count,
                "replayed": result.replayed,
                "record_ids": [str(row.id) for row in result.records],
                "processed_at": datetime.now(UTC).isoformat(),
            },
        )
        destination = paths["processed"] / safe_name
        os.replace(source, destination)
        return HandoffInboxItem(
            filename=safe_name,
            status="processed",
            size_bytes=destination.stat().st_size,
            modified_at=datetime.fromtimestamp(destination.stat().st_mtime, tz=UTC),
            channel_profile_id=str(result.batch.channel_profile_id),
            batch_key=result.batch.batch_key,
            record_count=result.batch.record_count,
            receipt=receipt,
        )
    except Exception as exc:
        receipt = _write_receipt(
            paths,
            safe_name,
            _failure_receipt(filename=safe_name, error=exc, parsed=parsed),
        )
        destination = paths["failed"] / safe_name
        if source.exists():
            os.replace(source, destination)
        return HandoffInboxItem(
            filename=safe_name,
            status="failed",
            size_bytes=destination.stat().st_size if destination.exists() else 0,
            modified_at=(
                datetime.fromtimestamp(destination.stat().st_mtime, tz=UTC)
                if destination.exists()
                else datetime.now(UTC)
            ),
            channel_profile_id=(
                str(parsed.channel_profile_id) if parsed is not None else None
            ),
            batch_key=parsed.batch_key if parsed is not None else None,
            record_count=len(parsed.records) if parsed is not None else None,
            error=str(exc)[:4000],
            receipt=receipt,
        )


def process_handoff_inbox(
    *,
    root: Path | None = None,
    limit: int = 50,
) -> list[HandoffInboxItem]:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    paths = _paths(root)
    pending = sorted(
        (
            path
            for path in paths["incoming"].glob("*.json")
            if _is_regular_handoff_file(path)
        ),
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    return [
        process_handoff_file(path.name, root=root)
        for path in pending[:limit]
    ]


def handoff_inbox_summary(*, root: Path | None = None) -> dict[str, Any]:
    paths = _paths(root)
    counts = {
        status: sum(
            1
            for path in paths[status].glob("*.json")
            if _is_regular_handoff_file(path)
        )
        for status in ("incoming", "processed", "failed")
    }
    return {
        "root": str(paths["root"]),
        "incoming": str(paths["incoming"]),
        "counts": counts,
        "max_file_bytes": _MAX_HANDOFF_BYTES,
    }

