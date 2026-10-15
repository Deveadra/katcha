"""No live Docker or OCI access: injected read-only host observations."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from katcha.ops import production_rollout_preflight as guard

OLD = "a" * 40
NEW = "b" * 40
IMAGE_IDS = {
    "katcha-control": "1",
    "katcha-ingest": "2",
    "katcha-renderer": "3",
    "katcha-production": "4",
    "postgres:16-alpine": "5",
    "temporalio/auto-setup:1.27.2": "6",
    "cloudflare/cloudflared:2026.9.2": "7",
    "katcha-control-new": "8",
}
THIRD = {"postgres", "temporal", "cloudflared"}


def fixture(tmp_path: Path, api_sha: str | None = None):
    env = tmp_path / "prod.env"
    env.write_text(
        f"KATCHA_RELEASE_SHA={OLD}\n"
        + (f"KATCHA_API_RELEASE_SHA={api_sha}\n" if api_sha else "")
        + "SECRET_TOKEN=very-sensitive\n",
        encoding="utf-8",
    )
    image_rows = {}
    container_rows = []
    for index, (service, image) in enumerate(guard.SERVICES.items()):
        tag = guard._expected_ref(service, OLD, api_sha or OLD)
        variant = image + ("-new" if service == "api" and api_sha else "")
        image_id = f"sha256:{IMAGE_IDS[variant] * 64}"
        labels = {
            "com.docker.compose.project": guard.PROJECT,
            "com.docker.compose.service": service,
        }
        if service not in THIRD:
            labels["org.opencontainers.image.revision"] = (
                api_sha if service == "api" and api_sha else OLD
            )
        container_rows.append({
            "Id": f"{index + 1:x}" * 12,
            "Image": image_id,
            "Config": {"Image": tag, "Labels": labels},
            "State": {
                "Running": True, "Status": "running", "Health": {"Status": "healthy"}
            },
        })
        expected_digest = (
            f"{guard.REGISTRY}/{image}@sha256:{IMAGE_IDS[variant] * 64}"
            if service not in THIRD else f"{tag}@sha256:{IMAGE_IDS[variant] * 64}"
        )
        image_rows[image_id] = {
            "Id": image_id,
            "Architecture": "arm64",
            "Os": "linux",
            "Config": {"Labels": {"org.opencontainers.image.revision": (
                api_sha if service == "api" and api_sha else OLD
            )}},
            "RepoDigests": [expected_digest],
        }

    calls = []

    def command(*args):
        calls.append(args)
        if args == ("systemctl", "is-active", "katcha.service"):
            return "active"
        if args == ("systemctl", "show", "--property=MainPID", "--value", "katcha.service"):
            return "1234"
        if args[:2] == ("docker", "ps"):
            return "\n".join(row["Id"] for row in container_rows)
        if args[:2] == ("docker", "inspect"):
            return json.dumps(container_rows)
        if args[:3] == ("docker", "image", "inspect"):
            return json.dumps(list(image_rows.values()))
        raise AssertionError(f"Unexpected command: {args}")

    def fetch(url):
        if url.endswith("/ready"):
            return {"status": "ok"}
        if url.endswith("/release"):
            return {"release_sha": api_sha or OLD}
        raise AssertionError(url)

    return env, command, fetch, container_rows, image_rows, calls


def test_valid_production_is_attested_for_review_only(tmp_path):
    env, command, fetch, _, _, calls = fixture(tmp_path)
    observed = guard.inspect(env, command=command, fetch=fetch)
    assert observed["status"] == "ATTESTED_FOR_REVIEW"
    assert observed["service_count"] == 9
    assert observed["global_sha"] == OLD
    assert observed["api_sha"] == OLD
    assert len(observed["services"]) == 9
    assert "registry_digest" in observed["services"]["api"]
    assert "No production deployment is authorized or performed" in observed["limitations"]
    assert all(args[0] in {"systemctl", "docker"} for args in calls)
    assert all("up" not in args and "restart" not in args for args in calls)
    assert "very-sensitive" not in json.dumps(observed)


def test_optional_api_hotfix_is_not_applied_to_workers(tmp_path):
    env, command, fetch, _, _, _ = fixture(tmp_path, NEW)
    result = guard.inspect(env, command=command, fetch=fetch)
    assert result["api_sha"] == NEW
    assert result["services"]["api"]["image"].endswith(NEW)
    assert result["services"]["worker"]["image"].endswith(OLD)


def test_missing_or_extra_containers_block(tmp_path):
    env, _, fetch, containers, images, _ = fixture(tmp_path)
    containers.pop()

    def command(*args):
        if args[0] == "systemctl":
            return "active" if args[1] == "is-active" else "1234"
        if args[:2] == ("docker", "ps"):
            return "\n".join(row["Id"] for row in containers)
        if args[:2] == ("docker", "inspect"):
            return json.dumps(containers)
        return json.dumps(list(images.values()))

    with pytest.raises(guard.PreflightBlocked, match="container count"):
        guard.inspect(env, command=command, fetch=fetch)


@pytest.mark.parametrize("mutation,message", [
    (lambda rows: rows[0]["State"].update(Running=False), "not running"),
    (lambda rows: rows[0]["State"]["Health"].update(Status="unhealthy"), "not healthy"),
    (lambda rows: rows[0]["Config"].update(Image="bad-image"), "differs"),
    (lambda rows: rows[0]["Config"]["Labels"].update(
        {"com.docker.compose.project": "untrusted"}), "ownership"),
    (lambda rows: rows[-1]["Config"]["Labels"].update(
        {"com.docker.compose.service": "api"}), "duplicate or unexpected"),
])
def test_bad_container_state_blocks(tmp_path, mutation, message):
    env, _, fetch, rows, image_rows, _ = fixture(tmp_path)
    mutation(rows)

    def command(*args):
        if args[0] == "systemctl":
            return "active" if args[1] == "is-active" else "1234"
        if args[:2] == ("docker", "ps"):
            return "\n".join(r["Id"] for r in rows)
        if args[:2] == ("docker", "inspect"):
            return json.dumps(rows)
        return json.dumps(list(image_rows.values()))

    with pytest.raises(guard.PreflightBlocked, match=message):
        guard.inspect(env, command=command, fetch=fetch)


def test_image_arch_or_digest_absence_blocks(tmp_path):
    env, _, fetch, rows, image_rows, _ = fixture(tmp_path)
    images = list(image_rows.values())

    def command(*args):
        if args[0] == "systemctl":
            return "active" if args[1] == "is-active" else "1234"
        if args[:2] == ("docker", "ps"):
            return "\n".join(r["Id"] for r in rows)
        if args[:2] == ("docker", "inspect"):
            return json.dumps(rows)
        return json.dumps(images)

    images[0]["Architecture"] = "amd64"
    with pytest.raises(guard.PreflightBlocked, match="architecture"):
        guard.inspect(env, command=command, fetch=fetch)
    images[0]["Architecture"] = "arm64"
    next(
        x for x in images if x["RepoDigests"][0].startswith(guard.REGISTRY)
    )["RepoDigests"] = []
    with pytest.raises(guard.PreflightBlocked, match="digest"):
        guard.inspect(env, command=command, fetch=fetch)


def test_unexpected_local_release_blocks(tmp_path):
    env, command, _, _, _, _ = fixture(tmp_path)
    with pytest.raises(guard.PreflightBlocked, match="release identity"):
        guard.inspect(env, command=command, fetch=lambda url: (
            {"status": "ok"} if url.endswith("/ready") else {"release_sha": NEW}
        ))


def test_host_command_rejects_mutating_docker_or_systemctl():
    for command in (
        ("docker", "compose", "up", "-d"),
        ("docker", "rm", "abc"),
        ("systemctl", "restart", "katcha.service"),
        ("systemctl", "stop", "katcha.service"),
        ("docker", "inspect", "-f", "{{.Config.Env}}", "abc"),
    ):
        with pytest.raises(guard.PreflightBlocked):
            guard.host_command(*command)


def test_whitelist_does_not_expose_secrets():
    with pytest.raises(guard.PreflightBlocked):
        guard.host_command("bash", "-c", "echo secret")
    with pytest.raises(guard.PreflightBlocked):
        guard.local_json("http://localhost:8000/v1/health/ready")


def test_release_candidate_requires_verified_main_comparison(tmp_path):
    env, command, fetch, _, _, _ = fixture(tmp_path)
    evidence = {
        "status": "REVIEW_REQUIRED",
        "candidate_sha": NEW,
        "ci_run_id": 12,
        "image_run_id": 15,
        "comparison": {"migration_changes": True, "worker_changes": True},
    }
    output = guard.assess_candidate(
        env, NEW, command=command, fetch=fetch, inspect_release=lambda _: evidence
    )
    assert output["status"] == "REVIEW_REQUIRED"
    assert output["candidate_sha"] == NEW
    assert output["comparison"]["migration_changes"] is True
    assert "No deployment performed" in output["warning"]
    with pytest.raises(guard.PreflightBlocked, match="eligible main"):
        guard.assess_candidate(
            env, "c" * 40, command=command, fetch=fetch,
            inspect_release=lambda _: evidence,
        )


def test_mixed_api_version_requires_supervised_review(tmp_path):
    env, command, fetch, _, _, _ = fixture(tmp_path, NEW)
    evidence = {
        "status": "REVIEW_REQUIRED",
        "candidate_sha": NEW,
        "ci_run_id": 12,
        "image_run_id": 15,
        "comparison": {"migration_changes": False},
    }
    with pytest.raises(guard.PreflightBlocked, match="mixed API"):
        guard.assess_candidate(
            env, NEW, command=command, fetch=fetch,
            inspect_release=lambda _: evidence,
        )


def test_missing_required_healthcheck_blocks(tmp_path):
    env, _, fetch, rows, images, _ = fixture(tmp_path)
    api = next(
        row for row in rows
        if row["Config"]["Labels"]["com.docker.compose.service"] == "api"
    )
    api["State"].pop("Health")

    def cmd(*args):
        if args[0] == "systemctl":
            return "active" if args[1] == "is-active" else "1234"
        if args[:2] == ("docker", "ps"):
            return "\n".join(row["Id"] for row in rows)
        if args[:2] == ("docker", "inspect"):
            return json.dumps(rows)
        return json.dumps(list(images.values()))

    with pytest.raises(guard.PreflightBlocked, match="healthcheck missing"):
        guard.inspect(env, command=cmd, fetch=fetch)


def test_container_inspect_must_match_ps_ids(tmp_path):
    env, _, fetch, rows, images, _ = fixture(tmp_path)
    rows[0]["Id"] = "f" * 64
    ids = [f"{index + 1:x}" * 12 for index in range(9)]

    def cmd(*args):
        if args[0] == "systemctl":
            return "active" if args[1] == "is-active" else "1234"
        if args[:2] == ("docker", "ps"):
            return "\n".join(ids)
        if args[:2] == ("docker", "inspect"):
            return json.dumps(rows)
        return json.dumps(list(images.values()))

    with pytest.raises(guard.PreflightBlocked, match="inventory identity mismatch"):
        guard.inspect(env, command=cmd, fetch=fetch)
