from __future__ import annotations

import re
from pathlib import Path


def test_temporal_workflows_do_not_request_object_typed_activity_results() -> None:
    orchestration = Path("src/katcha/orchestration")
    offenders: list[str] = []
    pattern = re.compile(r"result_type\s*=\s*dict\[str,\s*object\]")

    for path in sorted(orchestration.glob("*workflows.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))

    assert not offenders, (
        "Temporal cannot deserialize activity payloads using dict[str, object]; "
        f"remove that explicit result_type from: {offenders}"
    )


def test_workflow_result_hints_decode_actual_json_payloads() -> None:
    import importlib
    from typing import Any, get_args, get_origin

    from temporalio import workflow
    from temporalio.converter import value_to_type

    root = Path("src/katcha/orchestration")
    checked = set()
    for path in sorted(root.glob("*workflows.py")):
        module = importlib.import_module(f"katcha.orchestration.{path.stem}")
        for candidate in vars(module).values():
            if not isinstance(candidate, type):
                continue
            definition = workflow._Definition.from_class(candidate)
            if definition is None or definition.name in checked:
                continue
            checked.add(definition.name)
            result_hint = definition.ret_type
            if get_origin(result_hint) is not dict:
                continue
            value_hint = get_args(result_hint)[1]
            assert value_hint is not object, (
                f"{definition.name} would fail decoding its workflow/child result"
            )
            if value_hint is Any:
                payload = {"ok": True, "nested": {"ids": ["fixture"], "count": 2}}
                assert value_to_type(result_hint, payload) == payload
    assert "CommandSourcePrepareWorkflow" in checked
    assert "DiscoveryRunWorkflow" in checked


def test_discovery_lifecycle_arguments_decode_heterogeneous_details() -> None:
    from temporalio import activity
    from temporalio.converter import value_to_type

    from katcha.orchestration.discovery_activities import (
        record_command_source_prepare_lifecycle_activity,
        record_topic_watch_command_cycle_activity,
    )

    detail = {"count": 2, "error": None, "items": [{"success": True}]}
    for fn in [
        record_command_source_prepare_lifecycle_activity,
        record_topic_watch_command_cycle_activity,
    ]:
        hints = activity._Definition.must_from_callable(fn).arg_types
        assert value_to_type(hints[-1], detail) == detail


async def test_ingest_passthrough_decodes_activity_and_final_workflow_payloads(monkeypatch) -> None:
    from temporalio import workflow
    from temporalio.converter import DataConverter

    from katcha.orchestration.workflows import ClipIngestWorkflow

    converter = DataConverter.default
    ingest = {"clip_id": "saved-clip", "success": True}
    prepared = {
        "action": "prepared",
        "production_id": "saved-production",
        "created": True,
        "detail": {"count": 1, "error": None, "items": ["source-original"]},
    }
    calls = []

    async def execute(name, arg, **kwargs):
        calls.append((name, arg))
        value = {
            "ingest_source": ingest,
            "enqueue_ingested_analysis_activity": {"analysis_run_id": "analysis"},
            "prepare_authorized_passthrough_activity": prepared,
        }[name]
        payloads = await converter.encode([value])
        hint = kwargs.get("result_type")
        return (await converter.decode(payloads, [hint] if hint else None))[0]

    monkeypatch.setattr(workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow, "patched", lambda _name: True)
    result = await ClipIngestWorkflow().run("source")
    expected = {**ingest, "post_ingest": prepared}
    assert result == expected
    assert calls == [
        ("ingest_source", "source"),
        ("enqueue_ingested_analysis_activity", "saved-clip"),
        ("prepare_authorized_passthrough_activity", "source"),
    ]
    result_hint = workflow._Definition.must_from_class(ClipIngestWorkflow).ret_type
    assert (await converter.decode(await converter.encode([result]), [result_hint]))[0] == expected
