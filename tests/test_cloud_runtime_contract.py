from __future__ import annotations

from pathlib import Path

from katcha.ops import cloudflare_edge
from katcha.ops.production_runtime import validate

ROOT = Path(__file__).resolve().parents[1]


def valid_production_env() -> dict[str, str]:
    return {
        "KATCHA_ENV": "production",
        "KATCHA_RELEASE_SHA": "a" * 40,
        "KATCHA_LEADERSHIP_FENCE_MODE": "http",
        "KATCHA_LEADERSHIP_FENCE_URL": (
            "https://recovery.katcha.test/v1/fence/assert"
        ),
        "KATCHA_LEADERSHIP_FENCE_TOKEN": "fence-token-with-more-than-32-characters",
        "KATCHA_DEPLOYMENT_ID": "oci-a1-primary",
        "KATCHA_DEPLOYMENT_EPOCH": "7",
        "KATCHA_CLOUDFLARE_TUNNEL_TOKEN": "cloudflare-tunnel-token",
        "KATCHA_POSTGRES_PASSWORD": "database-secret-with-enough-entropy",
        "KATCHA_DATABASE_URL": (
            "postgresql+psycopg://katcha:database-secret-with-enough-entropy"
            "@postgres:5432/katcha"
        ),
        "KATCHA_S3_ENDPOINT_URL": "https://abc123.r2.cloudflarestorage.com",
        "KATCHA_S3_REGION": "auto",
        "KATCHA_S3_FORCE_PATH_STYLE": "false",
        "KATCHA_S3_ACCESS_KEY": "r2-access-key",
        "KATCHA_S3_SECRET_KEY": "r2-secret-key",
        "KATCHA_S3_BUCKET": "katcha-media-prod",
        "KATCHA_CREDENTIAL_ENCRYPTION_KEY": "x" * 44,
        "KATCHA_CONTROL_API_TOKEN": "control-token-with-more-than-32-characters",
        "KATCHA_CONTROL_PRINCIPALS": "[]",
        "KATCHA_YOUTUBE_REDIRECT_URI": (
            "https://katcha.test/v1/integrations/youtube/oauth/callback"
        ),
        "KATCHA_RENDER_BACKEND": "lambda",
        "KATCHA_AWS_EXPECTED_ACCOUNT_ID": "123456789012",
        "KATCHA_AWS_PROFILE": "katcha-automation",
        "KATCHA_REMOTION_LAMBDA_FUNCTION_NAME": "katcha-render",
        "KATCHA_REMOTION_LAMBDA_SERVE_URL": "https://render.katcha.test/remotion",
        "KATCHA_REMOTION_STAGING_BUCKET": "katcha-render-staging",
    }


def test_production_validator_accepts_safe_hosted_settings() -> None:
    assert validate(valid_production_env()) == []



def test_production_validator_requires_external_leadership_fence() -> None:
    values = valid_production_env()
    values.update(
        {
            "KATCHA_LEADERSHIP_FENCE_MODE": "disabled",
            "KATCHA_LEADERSHIP_FENCE_URL": "http://localhost:8788/v1/fence/assert",
            "KATCHA_LEADERSHIP_FENCE_TOKEN": "short",
            "KATCHA_DEPLOYMENT_ID": "local",
            "KATCHA_DEPLOYMENT_EPOCH": "0",
        }
    )
    errors = validate(values)
    assert any("FENCE_MODE" in error for error in errors)
    assert any("public HTTPS coordinator" in error for error in errors)
    assert any("32 characters" in error for error in errors)
    assert any("hosted deployment" in error for error in errors)
    assert any("at least 1" in error for error in errors)


def test_production_validator_requires_real_control_credential() -> None:
    values = valid_production_env()
    values["KATCHA_CONTROL_API_TOKEN"] = "CHANGE_ME"
    values["KATCHA_CONTROL_PRINCIPALS"] = "[]"
    assert any("CONTROL" in error for error in validate(values))


def test_production_validator_accepts_nonempty_principal_registry() -> None:
    values = valid_production_env()
    values["KATCHA_CONTROL_API_TOKEN"] = ""
    values["KATCHA_CONTROL_PRINCIPALS"] = (
        '[{"name":"operator","token":"principal-token-with-more-than-32-characters"}]'
    )
    assert validate(values) == []


def test_production_validator_rejects_local_defaults() -> None:
    values = valid_production_env()
    values.update(
        {
            "KATCHA_ENV": "development",
            "KATCHA_POSTGRES_PASSWORD": "katcha",
            "KATCHA_DATABASE_URL": "postgresql+psycopg://katcha:katcha@localhost:5432/katcha",
            "KATCHA_S3_ENDPOINT_URL": "http://minio:9000",
            "KATCHA_S3_ACCESS_KEY": "CHANGE_ME",
            "KATCHA_YOUTUBE_REDIRECT_URI": (
                "http://localhost:8000/v1/integrations/youtube/oauth/callback"
            ),
            "KATCHA_RENDER_BACKEND": "local",
        }
    )
    errors = validate(values)
    assert any("KATCHA_ENV" in error for error in errors)
    assert any("unsafe" in error for error in errors)
    assert any("Cloudflare R2" in error for error in errors)
    assert any("public HTTPS" in error for error in errors)
    assert any("must be lambda" in error for error in errors)


def test_production_compose_is_remote_and_immutable() -> None:
    compose = (ROOT / "deploy" / "docker-compose.production.yml").read_text()
    assert "build:" not in compose
    assert "KATCHA_RELEASE_SHA" in compose
    assert "127.0.0.1:8000:8000" in compose
    assert "5432:5432" not in compose
    assert "7233:7233" not in compose
    assert "\n  minio:" not in compose
    assert 'profiles: ["local-heavy-analysis"]' in compose
    assert "/srv/katcha/postgres:/var/lib/postgresql/data" in compose
    assert "/etc/katcha/aws:/home/katcha/.aws:ro" in compose
    assert "cloudflare/cloudflared:2026.9.2" in compose
    assert "KATCHA_CLOUDFLARE_TUNNEL_TOKEN" in compose
    assert "backup-writer:" in compose
    assert "backup-reader:" in compose
    assert "KATCHA_BACKUP_ENV_FILE" in compose
    assert "KATCHA_RESTORE_ENV_FILE" in compose


def test_oracle_control_plane_images_are_multiarch() -> None:
    workflow = (ROOT / ".github" / "workflows" / "runtime-images.yml").read_text()
    assert "docker/setup-qemu-action@v3" in workflow
    for image in (
        "katcha-control",
        "katcha-ingest",
        "katcha-analysis",
        "katcha-renderer",
        "katcha-production",
    ):
        section = workflow.split(f"image: {image}", 1)[1].split("- image:", 1)[0]
        assert "linux/amd64,linux/arm64" in section


def test_systemd_requires_durable_mount_and_restarts_supervisor() -> None:
    unit = (ROOT / "deploy" / "systemd" / "katcha.service").read_text()
    supervisor = (ROOT / "deploy" / "scripts" / "production-supervisor.sh").read_text()
    assert "RequiresMountsFor=/srv/katcha" in unit
    assert "Restart=on-failure" in unit
    assert "production-supervisor.sh" in unit
    assert "mountpoint -q" in supervisor
    assert 'PYTHONPATH="${ROOT}/src' in supervisor
    assert "/etc/katcha/aws/config" in supervisor
    assert "docker-compose.aws-roles-anywhere.yml" not in supervisor



def test_disaster_backup_units_are_durable_and_scheduled() -> None:
    backup_service = (
        ROOT / "deploy" / "systemd" / "katcha-backup.service"
    ).read_text()
    backup_timer = (
        ROOT / "deploy" / "systemd" / "katcha-backup.timer"
    ).read_text()
    restore_service = (
        ROOT / "deploy" / "systemd" / "katcha-restore-test.service"
    ).read_text()
    restore_timer = (
        ROOT / "deploy" / "systemd" / "katcha-restore-test.timer"
    ).read_text()

    assert "RequiresMountsFor=/srv/katcha" in backup_service
    assert "EnvironmentFile=/etc/katcha/katcha.env" in backup_service
    assert "postgres-backup.sh" in backup_service
    assert "OnCalendar=hourly" in backup_timer
    assert "Persistent=true" in backup_timer
    assert "postgres-restore-test.sh" in restore_service
    assert "Sun *-*-* 04:15:00 UTC" in restore_timer
    assert "Persistent=true" in restore_timer



def test_break_glass_recovery_is_manual_explicit_and_ephemeral() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "oci-recovery.yml"
    ).read_text(encoding="utf-8")

    assert "secret_source:" in workflow
    assert "default: oci-vault" in workflow
    assert "- break-glass" in workflow
    assert (
        "github.event_name == 'workflow_dispatch' && "
        "inputs.secret_source || 'oci-vault'"
    ) in workflow
    assert (
        "if: github.event_name == 'workflow_dispatch' && "
        "inputs.secret_source == 'break-glass'"
    ) in workflow
    assert "Build ephemeral off-OCI break-glass handoff" in workflow
    assert "Delete ephemeral break-glass handoff" in workflow
    assert (
        "if: always() && github.event_name == 'workflow_dispatch' && "
        "inputs.secret_source == 'break-glass'"
    ) in workflow
    assert "KATCHA_BREAK_GLASS_HANDOFF_OBJECT_KEY" in workflow
    assert "create-handoff-from-escrow" in workflow
    assert "KATCHA_BREAK_GLASS_ESCROW_KEY" in workflow
    assert "BREAK_GLASS_PRODUCTION_ENV_B64" not in workflow


def test_break_glass_candidate_keeps_oci_vault_and_escrow_paths_separate() -> None:
    bootstrap = (
        ROOT / "deploy" / "cloud-init" / "oci-recovery-candidate.sh.tmpl"
    ).read_text(encoding="utf-8")

    assert 'case "$SECRET_SOURCE" in' in bootstrap
    assert "oci-vault)" in bootstrap
    assert "break-glass)" in bootstrap
    assert "install-handoff" in bootstrap
    assert '--fernet-key "$BREAK_GLASS_HANDOFF_KEY"' not in bootstrap
    assert (
        'KATCHA_BREAK_GLASS_HANDOFF_KEY="$BREAK_GLASS_HANDOFF_KEY"'
        in bootstrap
    )



def test_break_glass_escrow_drill_is_manual_and_non_oci() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "break-glass-escrow-drill.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "repository_dispatch" not in workflow
    assert "schedule:" not in workflow
    assert "create-handoff-from-escrow" in workflow
    assert "install-handoff" in workflow
    assert "production_runtime" in workflow
    assert "disaster_recovery_validate" in workflow
    assert "Delete one-time handoff" in workflow
    assert "oci " not in workflow
