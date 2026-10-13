"""Regression contracts for safe GHCR publication after GitHub CI."""

from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "runtime-images.yml"
)


def test_release_images_wait_for_successful_main_ci() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    triggers = source.split("\npermissions:", 1)[0]
    assert "workflow_run:" in triggers
    assert 'workflows: ["CI"]' in triggers
    assert "types: [completed]" in triggers
    assert "branches: [main]" in triggers
    assert "\n  push:" not in triggers
    assert "\n  workflow_dispatch:" not in triggers
    assert 'run?.event !== "push"' in source
    assert 'run?.conclusion !== "success"' in source
    assert 'run?.head_branch !== "main"' in source
    assert "run?.head_repository?.full_name" in source


def test_release_requires_current_main_head_not_stale_ci() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "github.rest.repos.getBranch" in source
    assert "main.data.commit.sha !== run.head_sha" in source
    assert 'core.setOutput("has_images", "false")' in source
    assert 'releaseSha = run.head_sha' in source
    assert "core.setOutput(\"release_sha\", releaseSha)" in source


def test_pr_builds_cannot_publish_images() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "\n  pull_request:" in source
    assert "push: ${{ github.event_name == 'workflow_run' }}" in source
    assert "if: github.event_name != 'pull_request'" in source
    assert "context.eventName === \"pull_request\"" in source


def test_image_source_and_verification_are_exact_sha_pinned() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "ref: ${{ needs.plan.outputs.release_sha }}" in source
    assert "persist-credentials: false" in source
    assert "ghcr.io/deveadra/${{ matrix.image }}:${{ needs.plan.outputs.release_sha }}" in source
    assert "org.opencontainers.image.revision=${{ needs.plan.outputs.release_sha }}" in source
    assert "IMAGE_TAG: ${{ needs.plan.outputs.release_sha }}" in source
    assert 'test "${revision}" = "${IMAGE_TAG}"' in source
    assert "needs: [plan, build]" in source
    assert "if: github.event_name == 'workflow_run' && needs.build.result == 'success'" in source
