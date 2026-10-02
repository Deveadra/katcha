from __future__ import annotations

import ast
from pathlib import Path

from katcha.orchestration.recovery_registry import (
    WORKFLOW_RECOVERY_CONTRACTS,
    RecoveryMode,
)

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION = ROOT / "src" / "katcha" / "orchestration"


def _workflow_definitions() -> set[str]:
    names: set[str] = set()
    for path in sorted(ORCHESTRATION.glob("*workflows.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Attribute)
                    and isinstance(decorator.value, ast.Name)
                    and decorator.value.id == "workflow"
                    and decorator.attr == "defn"
                ):
                    names.add(node.name)
    return names


def test_every_temporal_workflow_has_explicit_recovery_contract() -> None:
    discovered = _workflow_definitions()
    registered = set(WORKFLOW_RECOVERY_CONTRACTS)
    assert registered == discovered, {
        "missing": sorted(discovered - registered),
        "stale": sorted(registered - discovered),
    }


def test_persisted_user_work_is_reconciled() -> None:
    for workflow in (
        "ClipIngestWorkflow",
        "ClipAnalysisWorkflow",
        "ShortProductionWorkflow",
        "RankedShortEpisodeEditorialWorkflow",
        "LongformCompilationWorkflow",
        "StagedBrandPreviewWorkflow",
        "YouTubePublicationWorkflow",
        "YouTubePackagingActivationWorkflow",
        "DiscoveryRunWorkflow",
        "CommandGoalWorkflow",
    ):
        assert (
            WORKFLOW_RECOVERY_CONTRACTS[workflow].mode
            == RecoveryMode.PERSISTED_RECONCILE
        )


def test_continuous_schedules_are_reconstructed_from_persisted_config() -> None:
    for workflow in (
        "TopicWatchScheduleWorkflow",
        "ChannelIntelligenceScheduleWorkflow",
        "ChannelTrendActivationScheduleWorkflow",
    ):
        assert (
            WORKFLOW_RECOVERY_CONTRACTS[workflow].mode
            == RecoveryMode.PERSISTED_RECONCILE
        )
