"""Deterministic portable export of project Media Bin references and evidence."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment, RightsEvidence
from katcha.db import session_scope
from katcha.editorial_models import (
    AcquisitionReceipt,
    EditorialProject,
    EditorialRevision,
    MediaAssetValidation,
    ProjectMediaItem,
)
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, SourceItem
from katcha.services.editorial_projects import EditorialNotFound

MAX_ASSET_BYTES = 200 * 1024 * 1024
MAX_EXPORT_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
_SENSITIVE_KEY = re.compile(
    r"(?:token|secret|password|authorization|credential|signature|private[_-]?key|api[_-]?key)",
    re.IGNORECASE,
)
_SENSITIVE_QUERY = re.compile(
    r"(?:token|secret|auth|signature|sig|credential|key|expires|x-amz-)", re.I
)


def _portable_url(value: str | None) -> str | None:
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return value
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if any(_SENSITIVE_QUERY.search(key) for key, _ in query):
            query = []
        return urlunsplit((parsed.scheme, host, parsed.path, urlencode(query), ""))
    except ValueError:
        return None


def _portable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _portable(item)
            for key, item in value.items()
            if not _SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, (list, tuple, set)):
        return [_portable(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return _portable_url(value)
    return value


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _zip_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    archive.writestr(_zip_info(name), data)


def _zip_manifest(archive: zipfile.ZipFile, manifest: dict[str, Any]) -> None:
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    size = 0
    with archive.open(_zip_info("manifest.json"), "w") as target:
        for chunk in encoder.iterencode(_portable(manifest)):
            data = chunk.encode("utf-8")
            size += len(data)
            if size > MAX_METADATA_BYTES:
                raise ValueError("Project export metadata exceeds the supported size")
            target.write(data)


def _file_name(sha256: str, mime: str | None) -> str:
    extension = {
        "video/mp4": "mp4",
        "video/webm": "webm",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "audio/ogg": "ogg",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }.get((mime or "").casefold(), "bin")
    return f"media/{sha256[:2]}/{sha256}.{extension}"


def _build_project_media_export(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    revision: int,
    *,
    store: ObjectStore | None = None,
    directory: Path,
) -> Path:
    """Return a stable ZIP with references, evidence and only verified cleared bytes."""
    object_store = store or ObjectStore()
    with session_scope() as session:
        project = session.get(EditorialProject, project_id)
        if project is None or project.channel_profile_id != channel_id:
            raise EditorialNotFound("Editorial project not found in this channel")
        project_revision = session.get(EditorialRevision, (project_id, revision))
        if project_revision is None:
            raise EditorialNotFound("Editorial project revision not found in this channel")
        items = list(
            session.scalars(
                select(ProjectMediaItem)
                .where(
                    ProjectMediaItem.project_id == project_id,
                    ProjectMediaItem.channel_profile_id == channel_id,
                    ProjectMediaItem.revision == revision,
                )
                .order_by(ProjectMediaItem.created_at, ProjectMediaItem.id)
            )
        )
        manifest_items: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        media_files: dict[str, Path] = {}
        total_media_bytes = 0
        clearance_snapshots: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}

        for item in items:
            clip = session.get(Clip, item.clip_id) if item.clip_id else None
            assessment = (
                session.get(RightsAssessment, item.rights_assessment_id)
                if item.rights_assessment_id
                else None
            )
            if assessment is not None:
                rights_candidate = session.get(
                    DiscoveryCandidate, assessment.discovery_candidate_id
                )
                rights_owner = (
                    (rights_candidate.candidate_metadata or {}).get("channel_profile_id")
                    if rights_candidate
                    else None
                )
                rights_source = (
                    session.get(SourceItem, rights_candidate.source_item_id)
                    if rights_candidate and rights_candidate.source_item_id
                    else None
                )
                latest_assessment = (
                    session.scalar(
                        select(RightsAssessment)
                        .where(RightsAssessment.discovery_candidate_id == rights_candidate.id)
                        .order_by(RightsAssessment.version.desc())
                    )
                    if rights_candidate is not None
                    else None
                )
                assessment_matches_item = rights_source is not None and (
                    (clip is not None and rights_source.clip_id == clip.id)
                    or (
                        item.external_reference_url is not None
                        and item.external_reference_url
                        in {rights_source.source_url, rights_source.canonical_url}
                    )
                )
                if (
                    str(rights_owner) != str(channel_id)
                    or not assessment_matches_item
                    or latest_assessment is None
                    or latest_assessment.id != assessment.id
                    or assessment.version != item.rights_assessment_version
                ):
                    assessment = None
            rights_evidence = []
            if assessment is not None and assessment.version == item.rights_assessment_version:
                rights_evidence = list(
                    session.scalars(
                        select(RightsEvidence)
                        .where(RightsEvidence.rights_assessment_id == assessment.id)
                        .order_by(RightsEvidence.captured_at, RightsEvidence.id)
                    )
                )

            source_url = _portable_url(item.external_reference_url)
            candidate_rows = []
            linked_source = None
            if clip is not None:
                linked_sources = list(
                    session.scalars(
                        select(SourceItem)
                        .where(SourceItem.clip_id == clip.id)
                        .order_by(SourceItem.id)
                    )
                )
                linked_source = next(
                    (
                        source
                        for source in linked_sources
                        if str((source.source_metadata or {}).get("channel_profile_id"))
                        == str(channel_id)
                    ),
                    None,
                )
                if source_url is None and linked_source is not None:
                    source_url = _portable_url(linked_source.canonical_url)
                if source_url is None and linked_source is not None:
                    source_url = _portable_url(linked_source.source_url)
                candidate_rows = [
                    row
                    for row in session.execute(
                        select(DiscoveryCandidate, SourceItem, AcquisitionReceipt)
                        .join(SourceItem, DiscoveryCandidate.source_item_id == SourceItem.id)
                        .join(
                            AcquisitionReceipt,
                            AcquisitionReceipt.candidate_id == DiscoveryCandidate.id,
                        )
                        .where(
                            SourceItem.clip_id == clip.id,
                            AcquisitionReceipt.channel_profile_id == channel_id,
                            AcquisitionReceipt.status == "succeeded",
                        )
                        .order_by(AcquisitionReceipt.created_at, AcquisitionReceipt.id)
                    )
                    if str((row[0].candidate_metadata or {}).get("channel_profile_id"))
                    == str(channel_id)
                ]
                if candidate_rows and source_url is None:
                    source_url = _portable_url(candidate_rows[0][0].canonical_url)

            receipt_evidence: dict[str, Any] | None = None
            media_path: str | None = None
            gap_reason: str | None = None
            for _candidate, _source, receipt in candidate_rows:
                validations = list(
                    session.scalars(
                        select(MediaAssetValidation)
                        .where(
                            MediaAssetValidation.receipt_id == receipt.id,
                            MediaAssetValidation.channel_profile_id == channel_id,
                        )
                        .order_by(MediaAssetValidation.probe_version.desc())
                    )
                )
                validation = validations[0] if validations else None
                if validation is not None and (
                    validation.status != "validated"
                    or validation.actual_sha256 != receipt.sha256
                    or validation.expected_sha256 != receipt.sha256
                ):
                    validation = None
                receipt_evidence = {
                    "status": receipt.status,
                    "sha256": receipt.sha256,
                    "mime": receipt.mime,
                    "bytes_received": receipt.bytes_received,
                    "source_version": receipt.source_version,
                    "request_time": receipt.request_time,
                    "response_time": receipt.response_time,
                    "validation": (
                        {
                            "status": validation.status,
                            "probe_version": validation.probe_version,
                            "sha256": validation.actual_sha256,
                            "mime": validation.mime,
                            "size_bytes": validation.size_bytes,
                            "duration_seconds": validation.duration_seconds,
                            "width": validation.width,
                            "height": validation.height,
                            "probe": validation.probe,
                        }
                        if validation is not None
                        else None
                    ),
                    "download_terms": receipt.download_terms,
                    "authorization": {
                        "provider": (receipt.authorization_scope or {}).get("provider"),
                        "operations": (receipt.authorization_scope or {}).get("operations", []),
                        "evidence_refs": (receipt.authorization_scope or {}).get(
                            "evidence_refs", []
                        ),
                    },
                }
                cleared = (
                    item.status in {"acquired", "ready"}
                    and item.rights_status == "production_cleared"
                    and assessment is not None
                    and assessment.version == item.rights_assessment_version
                    and assessment.production_eligible
                )
                if not cleared:
                    gap_reason = "production_clearance_required"
                elif validation is None:
                    gap_reason = "validated_acquisition_required"
                elif not receipt.object_store_key:
                    gap_reason = "stored_media_missing"
                elif validation.size_bytes > MAX_ASSET_BYTES:
                    gap_reason = "asset_size_limit"
                elif clip is None or clip.sha256 != receipt.sha256:
                    gap_reason = "clip_content_hash_mismatch"
                else:
                    proposed_path = _file_name(receipt.sha256 or "", validation.mime)
                    if proposed_path in media_files:
                        media_path = proposed_path
                    else:
                        temp_path = directory / uuid.uuid4().hex
                        actual_size = 0
                        digest = hashlib.sha256()
                        try:
                            with temp_path.open("wb") as output:
                                for chunk in object_store.iter_bytes(
                                    receipt.object_store_key, chunk_size=1024 * 1024
                                ):
                                    actual_size += len(chunk)
                                    if actual_size > MAX_ASSET_BYTES:
                                        raise ValueError("asset_size_limit")
                                    if total_media_bytes + actual_size > MAX_EXPORT_BYTES:
                                        raise ValueError("export_size_limit")
                                    digest.update(chunk)
                                    output.write(chunk)
                        except ValueError as exc:
                            gap_reason = (
                                str(exc)
                                if str(exc) in {"asset_size_limit", "export_size_limit"}
                                else "stored_media_unavailable"
                            )
                        except Exception:
                            gap_reason = "stored_media_unavailable"
                        else:
                            actual = digest.hexdigest()
                            latest_rights = (
                                session.execute(
                                    select(
                                        RightsAssessment.id, RightsAssessment.production_eligible
                                    )
                                    .where(
                                        RightsAssessment.discovery_candidate_id
                                        == assessment.discovery_candidate_id
                                    )
                                    .order_by(RightsAssessment.version.desc())
                                ).first()
                                if assessment is not None
                                else None
                            )
                            if (
                                actual != receipt.sha256
                                or actual != validation.actual_sha256
                                or actual_size != validation.size_bytes
                            ):
                                gap_reason = "stored_media_hash_changed"
                            elif (
                                assessment is None
                                or latest_rights is None
                                or latest_rights[0] != assessment.id
                                or not latest_rights[1]
                            ):
                                gap_reason = "production_clearance_required"
                            else:
                                media_path = proposed_path
                                media_files[media_path] = temp_path
                                total_media_bytes += actual_size
                        if media_path is None:
                            temp_path.unlink(missing_ok=True)
                if media_path is not None or gap_reason is not None:
                    break
            if clip is not None and not candidate_rows:
                gap_reason = "authorized_validated_receipt_missing"

            source_metadata = dict(item.source_metadata or {})
            provenance = dict(item.provenance or {})
            if clip is not None:
                source_metadata.setdefault("clip_sha256", clip.sha256)
                source_metadata.setdefault("clip_metadata", clip.media_metadata or {})
            entry = {
                "id": item.id,
                "project_id": item.project_id,
                "revision": item.revision,
                "channel_profile_id": item.channel_profile_id,
                "kind": item.kind,
                "status": item.status,
                "rights_status": item.rights_status,
                "rights_assessment": (
                    {
                        "id": assessment.id,
                        "version": assessment.version,
                        "rights_basis": assessment.rights_basis,
                        "rights_lane": assessment.rights_lane,
                        "rights_gate": assessment.rights_gate,
                        "audio_status": assessment.audio_status,
                        "originality_gate": assessment.originality_gate,
                        "production_eligible": assessment.production_eligible,
                        "risk_flags": assessment.risk_flags,
                        "reason": assessment.reason,
                        "evidence": [
                            {
                                "evidence_type": ev.evidence_type,
                                "source_url": _portable_url(ev.source_url),
                                "content_sha256": ev.content_sha256,
                                "terms_version": ev.terms_version,
                                "metadata": ev.evidence_metadata,
                                "captured_at": ev.captured_at,
                            }
                            for ev in rights_evidence
                        ],
                    }
                    if assessment is not None
                    else None
                ),
                "requirement_ids": item.requirement_ids,
                "searchable_tags": item.searchable_tags,
                "provenance": provenance,
                "source_metadata": source_metadata,
                "source_url": source_url,
                "external_reference_url": _portable_url(item.external_reference_url),
                "source_in_seconds": item.source_in_seconds,
                "source_out_seconds": item.source_out_seconds,
                "notes": item.notes,
                "created_at": item.created_at,
                "clip_sha256": clip.sha256 if clip else None,
                "acquisition": receipt_evidence,
                "media_path": media_path,
                "gap_reason": gap_reason,
            }
            if media_path is not None and assessment is not None:
                clearance_snapshots[str(item.id)] = (
                    assessment.discovery_candidate_id,
                    assessment.id,
                )
            manifest_items.append(entry)
            links.append(
                {
                    "item_id": str(item.id),
                    "kind": item.kind,
                    "source_url": source_url or "",
                    "sha256": (receipt_evidence or {}).get("sha256") or "",
                    "rights_status": item.rights_status,
                    "media_path": media_path or "",
                    "gap_reason": gap_reason or "",
                }
            )

        # Recheck every included item after all downloads, before publishing the archive.
        for entry, link in zip(manifest_items, links, strict=True):
            snapshot = clearance_snapshots.get(str(entry["id"]))
            if snapshot is None:
                continue
            latest = session.execute(
                select(RightsAssessment.id, RightsAssessment.production_eligible)
                .where(RightsAssessment.discovery_candidate_id == snapshot[0])
                .order_by(RightsAssessment.version.desc())
            ).first()
            if latest is None or latest[0] != snapshot[1] or not latest[1]:
                entry["media_path"] = None
                entry["gap_reason"] = "production_clearance_required"
                link["media_path"] = ""
                link["gap_reason"] = "production_clearance_required"
        included_paths = {entry["media_path"] for entry in manifest_items if entry["media_path"]}
        media_files = {name: path for name, path in media_files.items() if name in included_paths}

        manifest = {
            "schema": "katcha-project-media-export-v1",
            "project_id": project.id,
            "channel_profile_id": channel_id,
            "revision": revision,
            "project_input_digest": project.input_digest,
            "project_brief": project.brief,
            "revision_digest": project_revision.digest,
            "draft": project_revision.draft,
            "items": manifest_items,
        }
    with tempfile.NamedTemporaryFile(
        prefix="katcha-project-export-", suffix=".zip", delete=False
    ) as output:
        archive_path = Path(output.name)
        try:
            with zipfile.ZipFile(
                _LimitedZipFile(output), mode="w", compression=zipfile.ZIP_STORED
            ) as archive:
                _zip_manifest(archive, manifest)
                with (
                    archive.open(_zip_info("references/links.csv"), "w") as binary,
                    io.TextIOWrapper(binary, encoding="utf-8", newline="") as text,
                ):
                    writer = csv.DictWriter(
                        text,
                        fieldnames=[
                            "item_id",
                            "kind",
                            "source_url",
                            "sha256",
                            "rights_status",
                            "media_path",
                            "gap_reason",
                        ],
                        lineterminator="\n",
                    )
                    writer.writeheader()
                    writer.writerows(links)
                _zip_entry(
                    archive,
                    "README.txt",
                    (
                        b"Katcha project media export. Physical media is included only "
                        b"with matching "
                        b"validated acquisition evidence and production clearance. "
                        b"See manifest.json "
                        b"for gaps.\n"
                    ),
                )
                for name in sorted(media_files):
                    info = _zip_info(name)
                    with media_files[name].open("rb") as source, archive.open(info, "w") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
        except BaseException:
            archive_path.unlink(missing_ok=True)
            raise
    return archive_path


class _LimitedZipFile:
    """Enforce the complete ZIP size, including metadata and its central directory."""

    def __init__(self, output: Any) -> None:
        self.output = output

    def write(self, data: bytes) -> int:
        if self.output.tell() + len(data) > MAX_EXPORT_BYTES:
            raise ValueError("Project export exceeds the supported archive size")
        return int(self.output.write(data))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.output, name)


def build_project_media_export(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    revision: int,
    *,
    store: ObjectStore | None = None,
) -> bytes:
    """Preserve the integration API; stage media and ZIP on disk before one bounded read."""
    archive_path = build_project_media_export_file(channel_id, project_id, revision, store=store)
    try:
        return archive_path.read_bytes()
    finally:
        archive_path.unlink(missing_ok=True)


def build_project_media_export_file(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    revision: int,
    *,
    store: ObjectStore | None = None,
) -> Path:
    """Build a bounded disk archive. The caller owns and must remove the returned file."""
    with tempfile.TemporaryDirectory(prefix="katcha-media-export-") as directory:
        return _build_project_media_export(
            channel_id, project_id, revision, store=store, directory=Path(directory)
        )
