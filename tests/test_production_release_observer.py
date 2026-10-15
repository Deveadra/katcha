"""Offline regression coverage for the non-deploying release observer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from katcha.ops import production_release_observer as observer
from katcha.ops import release_eligibility

OLD = "a" * 40
NEW = "b" * 40
API = "c" * 40


def env(tmp_path: Path, text: str = f"KATCHA_RELEASE_SHA={OLD}\n") -> Path:
    path = tmp_path / "production.env"
    path.write_text(text, encoding="utf-8")
    return path


def remote(
    old: str = OLD,
    new: str = NEW,
    paths: tuple[str, ...] = ("src/katcha/web/home.js",),
):
    def get(path: str):
        if path == "/branches/main":
            return {"commit": {"sha": new}}
        if path == f"/compare/{old}...{new}":
            return {
                "status": "ahead",
                "behind_by": 0,
                "total_commits": 1,
                "merge_base_commit": {"sha": old},
                "files": [{"filename": name, "status": "modified"} for name in paths],
            }
        raise AssertionError("unexpected GitHub path: " + path)

    return get


def approved(sha: str) -> dict:
    assert sha == NEW
    return {
        "status": "ELIGIBLE_FOR_PREDEPLOY_REVIEW",
        "sha": sha,
        "ci_run_id": 17,
        "image_run_id": 18,
    }


def test_matching_main_is_current_config_only(tmp_path: Path) -> None:
    calls = []

    def get(path):
        calls.append(path)
        return {"commit": {"sha": OLD}}

    result = observer.inspect(
        env(tmp_path),
        api_get=get,
        check_release=lambda sha: pytest.fail("unexpected release validation"),
    )
    assert result == {"status": "CURRENT", "configured_sha": OLD, "api_sha": OLD}
    assert calls == ["/branches/main"]


def test_eligible_release_requires_review_not_mutation(tmp_path: Path) -> None:
    status = observer.inspect(env(tmp_path), api_get=remote(), check_release=approved)
    assert status["status"] == "REVIEW_REQUIRED"
    assert status["configured_sha"] == OLD
    assert status["candidate_sha"] == NEW
    assert status["ci_run_id"] == 17
    assert status["comparison"]["changed_files"] == 1
    assert status["comparison"]["migration_changes"] is False
    assert status["comparison"]["requires_supervised_compatibility_review"] is True
    assert "deployment disabled" in status["note"]


def test_migrations_workers_and_infra_flagged(tmp_path: Path) -> None:
    files = (
        "migrations/versions/0065_example.py",
        "src/katcha/orchestration/worker.py",
        "deploy/systemd/katcha.service",
    )
    outcome = observer.inspect(
        env(tmp_path), api_get=remote(paths=files), check_release=approved
    )
    changes = outcome["comparison"]
    assert changes["migration_changes"]
    assert changes["worker_changes"]
    assert changes["infrastructure_changes"]


@pytest.mark.parametrize("value", [
    "KATCHA_RELEASE_SHA=short\n",
    "KATCHA_RELEASE_SHA=\n",
    f"KATCHA_RELEASE_SHA={OLD}\nKATCHA_RELEASE_SHA={OLD}\n",
    f"KATCHA_RELEASE_SHA={OLD}\nKATCHA_API_RELEASE_SHA=invalid\n",
])
def test_ambiguous_or_invalid_pins_fail_closed(tmp_path: Path, value: str) -> None:
    with pytest.raises(observer.ObserverError):
        observer.inspect(env(tmp_path, value), api_get=remote(), check_release=approved)


def test_api_pin_cannot_be_treated_as_global_release(tmp_path: Path) -> None:
    path = env(tmp_path, f"KATCHA_RELEASE_SHA={OLD}\nKATCHA_API_RELEASE_SHA={API}\n")
    result = observer.inspect(path, api_get=remote(new=OLD), check_release=approved)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["api_sha"] == API
    assert result["candidate_sha"] == OLD


def test_rejected_release_blocks(tmp_path: Path) -> None:
    def reject(_):
        raise release_eligibility.ReleaseNotEligible("CI failed")

    with pytest.raises(observer.ObserverError, match="CI failed"):
        observer.inspect(env(tmp_path), api_get=remote(), check_release=reject)


def test_mismatched_evidence_blocks(tmp_path: Path) -> None:
    def wrong_sha(_):
        return {
            "status": "ELIGIBLE_FOR_PREDEPLOY_REVIEW",
            "sha": OLD,
            "ci_run_id": 1,
            "image_run_id": 2,
        }

    with pytest.raises(observer.ObserverError, match="eligibility contract"):
        observer.inspect(env(tmp_path), api_get=remote(), check_release=wrong_sha)


def test_main_advancing_between_checks_blocks(tmp_path: Path) -> None:
    base = remote()
    seen = 0

    def get(path):
        nonlocal seen
        if path == "/branches/main":
            seen += 1
            return {"commit": {"sha": NEW if seen == 1 else API}}
        return base(path)

    with pytest.raises(observer.ObserverError, match="main advanced"):
        observer.inspect(env(tmp_path), api_get=get, check_release=approved)


def test_non_descendant_and_truncated_compare_block(tmp_path: Path) -> None:
    base = remote()

    def non_descendant(path):
        result = base(path)
        if path.startswith("/compare/"):
            result["merge_base_commit"]["sha"] = API
        return result

    with pytest.raises(observer.ObserverError, match="not descended"):
        observer.inspect(env(tmp_path), api_get=non_descendant, check_release=approved)

    def truncated(path):
        result = base(path)
        if path.startswith("/compare/"):
            result["files"] = [
                {"filename": f"web/{i}.js", "status": "modified"}
                for i in range(300)
            ]
        return result

    with pytest.raises(observer.ObserverError, match="truncated"):
        observer.inspect(env(tmp_path), api_get=truncated, check_release=approved)


def test_secrets_are_not_returned(tmp_path: Path) -> None:
    secret = "CONFIDENTIAL_NOT_A_RELEASE_SHA"
    result = observer.inspect(
        env(tmp_path, f"SECRET_TOKEN={secret}\nKATCHA_RELEASE_SHA={OLD}\n"),
        api_get=remote(),
        check_release=approved,
    )
    assert secret not in json.dumps(result)


def test_observer_timer_is_separate_from_production() -> None:
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/systemd/katcha-release-observer.service").read_text()
    timer = (root / "deploy/systemd/katcha-release-observer.timer").read_text()
    original_installer = (
        root / "deploy/scripts/install-production-units.sh"
    ).read_text()
    source = (root / "src/katcha/ops/production_release_observer.py").read_text()
    assert "python3 -m katcha.ops.production_release_observer" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "OnCalendar=hourly" in timer
    assert "katcha-release-observer" not in original_installer
    assert "subprocess" not in source
    assert "os.system" not in source


def test_install_requires_explicit_opt_in() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "deploy/scripts/install-release-observer.sh").read_text()
    assert 'MODE="${1:---help}"' in script
    assert '"--install"' in script and '"--enable"' in script
    assert 'if [[ "$MODE" == "--enable" ]]' in script
    assert "systemctl enable --now katcha-release-observer.timer" in script
    assert "systemctl restart katcha.service" not in script
    assert "docker compose" not in script
