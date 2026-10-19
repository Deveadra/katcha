"""Exercise release observations against real PostgreSQL in a disposable container.

Run with KATCHA_SCHEMA_TEST_CONTAINER=katcha-gate-0065-test. The explicit
container name prefix protects against accidentally resetting a production DB.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POLICY = "approve-additive-through-0065"


@pytest.fixture
def postgres_gate(monkeypatch):
    name = os.environ.get("KATCHA_SCHEMA_TEST_CONTAINER")
    if not name:
        pytest.skip("requires an explicitly selected disposable PostgreSQL container")
    if not name.startswith("katcha-gate-") or name == "katcha-production-postgres-1":
        pytest.fail("refusing to reset a non-test PostgreSQL container")
    spec = importlib.util.spec_from_file_location(
        "production_deploy_real_postgres", ROOT / "deploy/scripts/production-deploy.py"
    )
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)

    def sql(statement):
        result = subprocess.run(
            ["docker", "exec", "-i", name, "psql", "-X", "-qAt", "-v",
             "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres"],
            input=statement, text=True, capture_output=True, check=True,
        )
        return result.stdout.strip()

    def migration(revision):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        buf = io.StringIO()
        ctx = MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buf}
        )
        source = ROOT / "migrations/versions" / (revision + ".py")
        migration_spec = importlib.util.spec_from_file_location("schema_migration", source)
        target = importlib.util.module_from_spec(migration_spec)
        migration_spec.loader.exec_module(target)
        with Operations.context(ctx):
            target.upgrade()
        sql(buf.getvalue())
        sql("UPDATE alembic_version SET version_num = '" + revision + "'")

    def run_observation(*args, **kwargs):
        # Keep the actual deployer's fixed SQL, psql flags and shell quoting.
        assert args[:3] == ("docker", "exec", "katcha-production-postgres-1")
        result = subprocess.run(
            ["docker", "exec", "-e", "POSTGRES_USER=postgres", "-e",
             "POSTGRES_DB=postgres", name, *args[3:]],
            text=True, capture_output=True, check=True,
        )
        return result.stdout.strip()

    monkeypatch.setattr(app, "run", run_observation)
    sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public; "
        "CREATE TABLE alembic_version (version_num varchar(128) PRIMARY KEY); "
        "INSERT INTO alembic_version VALUES ('0063_authoritative_search_ledger'); "
        "CREATE TABLE acquisition_receipts (id uuid NOT NULL, "
        "channel_profile_id uuid NOT NULL, UNIQUE (id, channel_profile_id)); "
        "INSERT INTO acquisition_receipts VALUES "
        "('00000000-0000-0000-0000-000000000001', "
        "'00000000-0000-0000-0000-000000000002')")
    yield app, sql, migration
    sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public")


@pytest.mark.parametrize("base", ["0063", "0064", "0065"])
def test_real_postgres_accepts_only_reviewed_schema_chain(postgres_gate, base):
    app, sql, migrate = postgres_gate
    receipt_before = sql("TABLE acquisition_receipts")
    if base in {"0064", "0065"}:
        migrate("0064_application_ai_settings")
        sql("INSERT INTO application_ai_settings VALUES ('retained', 'secret-ciphertext')")
    if base == "0065":
        migrate("0065_acquisition_jobs")
    if base == "0065":
        app.database_release_preflight(False, "refuse")
    else:
        with pytest.raises(app.Blocked):
            app.database_release_preflight(False, "refuse")
        app.database_release_preflight(True, POLICY)
        if base == "0063":
            migrate("0064_application_ai_settings")
        migrate("0065_acquisition_jobs")
    app.database_release_postflight("refuse")
    assert sql("TABLE acquisition_receipts") == receipt_before
    if base != "0063":
        assert sql("TABLE application_ai_settings") == "retained|secret-ciphertext"


@pytest.mark.parametrize("drift", [
    "ALTER TABLE acquisition_jobs DROP CONSTRAINT ck_acquisition_job_status",
    "ALTER TABLE acquisition_jobs DROP CONSTRAINT ck_acquisition_job_progress; "
    "ALTER TABLE acquisition_jobs ADD CONSTRAINT ck_acquisition_job_progress "
    "CHECK (bytes_received >= 0)",
    "ALTER TABLE acquisition_jobs DROP CONSTRAINT fk_acquisition_job_receipt_channel; "
    "ALTER TABLE acquisition_jobs ADD CONSTRAINT fk_acquisition_job_receipt_channel "
    "FOREIGN KEY (receipt_id, channel_profile_id) "
    "REFERENCES acquisition_receipts(id, channel_profile_id) NOT VALID",
    "ALTER TABLE acquisition_jobs DROP CONSTRAINT fk_acquisition_job_receipt_channel; "
    "ALTER TABLE acquisition_jobs ADD CONSTRAINT fk_acquisition_job_receipt_channel "
    "FOREIGN KEY (receipt_id, channel_profile_id) "
    "REFERENCES acquisition_receipts(id, channel_profile_id) ON DELETE CASCADE",
    "ALTER TABLE acquisition_jobs ALTER COLUMN status TYPE varchar(64)",
    "ALTER TABLE acquisition_jobs ALTER COLUMN pending_result DROP NOT NULL",
    "ALTER TABLE acquisition_jobs ALTER COLUMN created_at DROP DEFAULT",
    "DROP INDEX ix_acquisition_jobs_status",
    "DROP INDEX ix_acquisition_jobs_status; "
    "CREATE INDEX ix_acquisition_jobs_status ON acquisition_jobs (phase)",
    "ALTER TABLE acquisition_jobs ADD COLUMN unexpected text",
    "ALTER TABLE application_ai_settings ADD COLUMN unexpected text",
    "UPDATE alembic_version SET version_num='0066_future'",
])
def test_real_postgres_rejects_schema_drift(postgres_gate, drift):
    app, sql, migrate = postgres_gate
    migrate("0064_application_ai_settings")
    migrate("0065_acquisition_jobs")
    app.database_release_postflight("refuse")
    sql(drift)
    with pytest.raises(app.Blocked):
        app.database_release_preflight(False, "refuse")
    with pytest.raises(app.Blocked):
        app.database_release_postflight("refuse")


@pytest.mark.parametrize("base", ["0063", "0064"])
def test_real_postgres_rejects_preexisting_additive_tables(postgres_gate, base):
    app, sql, migrate = postgres_gate
    if base == "0064":
        migrate("0064_application_ai_settings")
        sql("CREATE TABLE acquisition_jobs (id uuid)")
    else:
        sql("CREATE TABLE application_ai_settings (key text, value text)")
    with pytest.raises(app.Blocked):
        app.database_release_preflight(True, POLICY)
