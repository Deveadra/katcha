"""Regression contracts for safe GHCR publication after GitHub CI."""

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

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
    assert 'core.setFailed("Rejected image publication:' in source


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


def workflow_scripts() -> list[str]:
    """Extract the actual inline JS so behavior checks cannot drift from the gate."""
    source = WORKFLOW.read_text(encoding="utf-8")
    scripts = []
    for block in source.split("          script: |\n")[1:]:
        lines = []
        for line in block.splitlines():
            if line and not line.startswith("            "):
                break
            lines.append(line)
        scripts.append(textwrap.dedent("\n".join(lines)).strip())
    return scripts


def execute_script(script: str, payload: dict, current_sha: str) -> dict:
    harness = """
const fixture = JSON.parse(process.argv[1]);
const outputs = {};
const failures = [];
const context = {
  eventName: "workflow_run", sha: "b".repeat(40),
  repo: {owner: "Deveadra", repo: "katcha"}, payload: fixture.payload,
};
const core = {
  setOutput: (key, value) => { outputs[key] = value; },
  setFailed: (message) => { failures.push(message); }, info: () => {},
};
const github = { rest: { repos: { getBranch: async () => ({
  data: {commit: {sha: fixture.current_sha}},
}) }} };
process.env.RELEASE_SHA = "a".repeat(40);
(async () => {
""" + script + """
})().then(() => { process.stdout.write(JSON.stringify({outputs, failures})); })
.catch((error) => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        ["node", "-e", harness, json.dumps({"payload": payload, "current_sha": current_sha})],
        capture_output=True, text=True, check=True, timeout=15,
    )
    return json.loads(result.stdout)


def ci_payload(**changes: object) -> dict:
    run = {
        "event": "push", "conclusion": "success", "head_branch": "main",
        "head_repository": {"full_name": "Deveadra/katcha"},
        "head_sha": "a" * 40,
    }
    run.update(changes)
    return {"workflow_run": run}


def test_executed_publication_plan_uses_tested_sha() -> None:
    result = execute_script(workflow_scripts()[0], ci_payload(), "a" * 40)
    assert result["failures"] == []
    assert result["outputs"]["release_sha"] == "a" * 40
    assert len(json.loads(result["outputs"]["matrix"])["include"]) == 6


@pytest.mark.parametrize("changes", [
    {"event": "pull_request"}, {"event": "schedule"},
    {"conclusion": "failure"}, {"conclusion": "cancelled"},
    {"head_branch": "feature"}, {"head_repository": {"full_name": "foreign/katcha"}},
    {"head_sha": "main"},
])
def test_executed_publication_plan_rejects_untrusted_ci(changes: dict) -> None:
    result = execute_script(workflow_scripts()[0], ci_payload(**changes), "a" * 40)
    assert result["failures"]
    assert result["outputs"]["has_images"] == "false"
    assert result["outputs"]["release_sha"] == ""


def test_executed_publication_plan_rejects_stale_ci() -> None:
    result = execute_script(workflow_scripts()[0], ci_payload(), "b" * 40)
    assert result["failures"]
    assert result["outputs"]["has_images"] == "false"


def test_postbuild_gate_rejects_main_advancing_during_build() -> None:
    gate = workflow_scripts()[1]
    assert execute_script(gate, ci_payload(), "a" * 40)["failures"] == []
    assert execute_script(gate, ci_payload(), "b" * 40)["failures"]
    source = WORKFLOW.read_text(encoding="utf-8")
    assert source.index("push: false") < source.index("Recheck current main")
    assert source.index("Recheck current main") < source.index("Publish cached exact revision")
