"""Real PostgreSQL recovery evidence; every discovery adapter is an offline fixture.

CI supplies KATCHA_SEARCH_LEDGER_TEST_DATABASE_URL for an isolated PostgreSQL
service. No application configuration or production database is consulted.
"""

import multiprocessing
import os
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch
from katcha.editorial.source_intelligence.contracts import (
    ProviderPolicyV1,
    SearchIntentV1,
    SearchScopeV1,
)
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.planner import SearchCapability, plan_job

CAP = SearchCapability(
    "fixture_search", "v1", "fixture", 100, external=False, supports_negative_terms=True
)
SCOPE = SearchScopeV1(channel_id=UUID(int=1), project_id=UUID(int=2), revision_digest="a" * 64)
TABLES = (
    "source_search_ledger_identity",
    "source_search_candidates",
    "source_search_budgets",
    "source_search_jobs",
    "source_search_receipt_events",
)


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Recovery tests must not contact real providers")

    monkeypatch.setattr(httpx.Client, "request", forbidden)


def planned(requirement="req-1", policy=None, scope=SCOPE):
    policy = policy or ProviderPolicyV1(version="fixture-policy-v1")
    intent = SearchIntentV1(
        scope=scope,
        requirement_id=requirement,
        query_terms=("fixture",),
        semantic_brief="Offline fixture",
        source_purpose="official_reference",
        requested_by="fixture",
    )
    return plan_job(
        intent,
        provider_key=CAP.provider_key,
        provider_version=CAP.provider_version,
        policy=policy,
        max_results=2,
        capability=CAP,
    )


class FixtureAdapter:
    key = CAP.provider_key
    version = CAP.provider_version

    def __init__(self, calls=None, fail=False):
        self.calls = calls if calls is not None else []
        self.fail = fail

    def discover(self, query, cursor):
        self.calls.append("fixture")
        if self.fail:
            raise TimeoutError("Fixture ambiguous outcome")
        return DiscoveryBatch(
            items=(DiscoveredCandidate("https://example.org/fixture", external_id="fixture"),)
        )


def dispatch(ledger, job, adapter=None, policy=None, scope=SCOPE):
    return ledger.dispatch(
        job,
        scope=scope,
        policy=policy or ProviderPolicyV1(version="fixture-policy-v1"),
        adapter=adapter or FixtureAdapter(),
        capability=CAP,
    )


@pytest.fixture
def database():
    raw = os.environ.get("KATCHA_SEARCH_LEDGER_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("Requires isolated CI PostgreSQL service URL")
    url = make_url(raw)
    assert url.get_backend_name() == "postgresql", "Test service must be PostgreSQL"
    name = "search_ledger_test_" + uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url.set(database=name), pool_pre_ping=True)
    try:
        import katcha.search_ledger_models  # noqa: F401
        from katcha.db import Base

        Base.metadata.create_all(
            engine,
            tables=[
                table
                for table in Base.metadata.sorted_tables
                if table.name.startswith("source_search_")
            ],
        )
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()


@pytest.fixture
def initialized(database, tmp_path):
    from katcha.editorial.source_intelligence.postgres_jobs import (
        PostgresSearchJobLedger,
        import_sqlite_ledger,
    )

    source = tmp_path / "source.sqlite"
    SearchJobLedger(source)
    import_sqlite_ledger(database, source, recovery_generation=7, confirmed_quiesced=True)
    activate(database)
    return PostgresSearchJobLedger(database, recovery_generation=7, dispatch_enabled=True)


def activate(engine, generation=7):
    from katcha.editorial.source_intelligence.postgres_jobs import (
        activate_postgres_ledger,
        inspect_postgres_ledger,
    )

    report = inspect_postgres_ledger(engine)
    return activate_postgres_ledger(
        engine,
        expected_identity=report["ledger_id"],
        expected_seal=report["integrity_sha256"],
        expected_generation=report["recovery_generation"],
        recovery_generation=generation,
        completeness_evidence="Isolated fixture: complete source history; zero live calls",
        confirmed_quiesced=True,
    )


def reopen(engine, generation=7, enabled=True):
    from katcha.editorial.source_intelligence.postgres_jobs import PostgresSearchJobLedger

    return PostgresSearchJobLedger(engine, recovery_generation=generation, dispatch_enabled=enabled)


def test_restart_preserves_receipts_replay_candidates_and_ambiguous_budget(database, initialized):
    policy = ProviderPolicyV1(version="fixture-policy-v1", max_calls=2)
    success_job = planned(policy=policy)
    ambiguous_job = planned("ambiguous", policy)
    calls = []
    success = dispatch(initialized, success_job, FixtureAdapter(calls), policy)
    unknown = dispatch(initialized, ambiguous_job, FixtureAdapter(calls, fail=True), policy)
    assert unknown.status == "unknown"
    database.dispose()
    restarted = reopen(database)
    assert restarted.identity == initialized.identity
    assert dispatch(restarted, success_job, FixtureAdapter(calls), policy) == success
    assert dispatch(restarted, ambiguous_job, FixtureAdapter(calls), policy) == unknown
    assert restarted.candidates(success_job.job_id, scope=SCOPE)
    assert [r.status for r in restarted.history(ambiguous_job.job_id, scope=SCOPE)] == [
        "running",
        "unknown",
    ]
    assert (
        dispatch(restarted, planned("next", policy), FixtureAdapter(calls), policy).status
        == "quota_exceeded"
    )
    assert len(calls) == 2


def _crash_after_claim(url, payload, policy_payload):
    from katcha.editorial.source_intelligence.contracts import SourceSearchJobV1

    engine = create_engine(url)
    ledger = reopen(engine)
    receipt, execute = ledger._claim(
        SourceSearchJobV1.model_validate_json(payload),
        ProviderPolicyV1.model_validate_json(policy_payload),
        CAP,
        retry=False,
    )
    os._exit(73 if execute and receipt.status == "running" else 74)


def test_process_death_after_atomic_claim_never_reissues_provider_call(database, initialized):
    policy = ProviderPolicyV1(version="fixture-policy-v1", max_calls=1)
    job = planned(policy=policy)
    process = multiprocessing.get_context("fork").Process(
        target=_crash_after_claim,
        args=(
            database.url.render_as_string(hide_password=False),
            job.model_dump_json(),
            policy.model_dump_json(),
        ),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 73
    restarted = reopen(database)
    adapter = FixtureAdapter()
    assert dispatch(restarted, job, adapter, policy).status == "running"
    assert restarted.mark_interrupted(job.job_id, scope=SCOPE).status == "unknown"
    assert dispatch(restarted, job, adapter, policy).status == "unknown"
    assert (
        dispatch(restarted, planned("different", policy), adapter, policy).status
        == "quota_exceeded"
    )
    assert not adapter.calls


def _worker(url, marker):
    class ProcessAdapter(FixtureAdapter):
        def discover(self, query, cursor):
            with open(marker, "a", encoding="utf-8") as file:
                file.write("fixture-call\n")
            return super().discover(query, cursor)

    engine = create_engine(url)
    try:
        dispatch(reopen(engine), planned(), ProcessAdapter())
    finally:
        engine.dispose()


def test_independent_api_processes_claim_same_job_once(database, initialized, tmp_path):
    marker = tmp_path / "calls"
    processes = [
        multiprocessing.get_context("fork").Process(
            target=_worker, args=(database.url.render_as_string(hide_password=False), str(marker))
        )
        for _ in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    assert marker.read_text().splitlines() == ["fixture-call"]
    assert reopen(database).read(planned().job_id, scope=SCOPE).status == "succeeded"


def test_concurrent_different_jobs_reserve_shared_budget_atomically(database, initialized):
    policy = ProviderPolicyV1(version="fixture-policy-v1", max_calls=3, max_results=6)
    calls = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda i: dispatch(
                    reopen(database), planned(f"req-{i}", policy), FixtureAdapter(calls), policy
                ),
                range(12),
            )
        )
    assert len(calls) == 3
    assert sum(result.status == "succeeded" for result in results) == 3
    assert sum(result.status == "quota_exceeded" for result in results) == 9


def test_cross_channel_reads_history_candidates_and_dispatch_are_rejected(database, initialized):
    job = planned()
    dispatch(initialized, job)
    wrong = SCOPE.model_copy(update={"channel_id": UUID(int=3)})
    adapter = FixtureAdapter()
    for operation in (initialized.read, initialized.history, initialized.candidates):
        with pytest.raises(PermissionError):
            operation(job.job_id, scope=wrong)
    with pytest.raises(PermissionError):
        dispatch(reopen(database), job, adapter, scope=wrong)
    assert not adapter.calls


def dump_restore(engine, path):
    # Database and password originate exclusively from the test-service URL.
    for program in ("pg_dump", "pg_restore"):
        assert shutil.which(program), f"Required CI recovery executable missing: {program}"
    url = engine.url.set(drivername="postgresql")
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    connection_args = [
        "--host",
        url.host or "localhost",
        "--port",
        str(url.port or 5432),
        "--username",
        url.username or "katcha",
    ]
    subprocess.run(
        [
            "pg_dump",
            *connection_args,
            "--format=custom",
            "--no-owner",
            "--file",
            str(path),
            url.database,
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    return env, connection_args


def restore(engine, path, env, args):
    engine.dispose()
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    subprocess.run(
        [
            "pg_restore",
            *args,
            "--no-owner",
            "--exit-on-error",
            "--dbname",
            engine.url.database,
            str(path),
        ],
        env=env,
        check=True,
        capture_output=True,
    )


def test_actual_dump_restore_preserves_unknown_claim_receipts_and_budget(
    database, initialized, tmp_path
):
    policy = ProviderPolicyV1(version="fixture-policy-v1", max_calls=1)
    job = planned(policy=policy)
    receipt = dispatch(initialized, job, FixtureAdapter(fail=True), policy)
    identity = initialized.identity
    dump = tmp_path / "ledger.dump"
    env, args = dump_restore(database, dump)
    restore(database, dump, env, args)
    restored = reopen(database)
    adapter = FixtureAdapter()
    assert restored.identity == identity
    assert dispatch(restored, job, adapter, policy) == receipt
    assert [r.status for r in restored.history(job.job_id, scope=SCOPE)] == ["running", "unknown"]
    assert dispatch(restored, planned("new", policy), adapter, policy).status == "quota_exceeded"
    assert not adapter.calls


def test_old_complete_restore_blocks_new_generation_even_when_content_seal_valid(
    database, initialized, tmp_path
):
    from katcha.editorial.source_intelligence.postgres_jobs import SearchLedgerRecoveryError

    early = planned("pre-backup")
    dispatch(initialized, early)
    dump = tmp_path / "old.dump"
    env, args = dump_restore(database, dump)
    after = planned("lost-window")
    dispatch(initialized, after, FixtureAdapter(fail=True))
    restore(database, dump, env, args)
    restored = reopen(database, generation=8)
    assert restored.read(early.job_id, scope=SCOPE).status == "succeeded"
    assert restored.read(after.job_id, scope=SCOPE) is None
    adapter = FixtureAdapter()
    with pytest.raises(SearchLedgerRecoveryError):
        dispatch(restored, after, adapter)
    assert not adapter.calls


@pytest.mark.parametrize(
    "table",
    [
        "source_search_jobs",
        "source_search_budgets",
        "source_search_candidates",
        "source_search_receipt_events",
        "source_search_ledger_identity",
        "source_search_ledger_head",
    ],
)
def test_partial_restore_missing_rows_fails_closed(database, initialized, table):
    from katcha.editorial.source_intelligence.postgres_jobs import SearchLedgerRecoveryError

    dispatch(initialized, planned())
    with database.begin() as connection:
        connection.execute(text(f'DELETE FROM "{table}"'))
    adapter = FixtureAdapter()
    with pytest.raises(SearchLedgerRecoveryError):
        dispatch(reopen(database), planned("new"), adapter)
    assert not adapter.calls


def test_lost_database_does_not_bootstrap_or_fall_back_to_sqlite(database):
    from katcha.editorial.source_intelligence.postgres_jobs import SearchLedgerRecoveryError

    with pytest.raises(SearchLedgerRecoveryError):
        dispatch(reopen(database), planned())


def test_dispatch_defaults_disabled_but_reads_remain_available(database, initialized):
    from katcha.editorial.source_intelligence.postgres_jobs import SearchLedgerRecoveryError

    receipt = dispatch(initialized, planned())
    disabled = reopen(database, enabled=False)
    assert disabled.read(planned().job_id, scope=SCOPE) == receipt
    adapter = FixtureAdapter()
    with pytest.raises(SearchLedgerRecoveryError):
        dispatch(disabled, planned("new"), adapter)
    assert not adapter.calls


def test_sqlite_wal_import_preserves_historical_raw_records_identity_and_replay(database, tmp_path):
    from katcha.editorial.source_intelligence.postgres_jobs import import_sqlite_ledger

    source = tmp_path / "history.sqlite"
    sqlite_ledger = SearchJobLedger(source)
    success = dispatch(sqlite_ledger, planned())
    interrupted = planned("interrupted")
    sqlite_ledger._claim(
        interrupted, ProviderPolicyV1(version="fixture-policy-v1"), CAP, retry=False
    )
    # Keep a reader open so later committed rows remain in WAL, rather than
    # accidentally proving only a closed/checkpointed .sqlite-file copy.
    held = sqlite3.connect(source)
    held.execute("PRAGMA journal_mode=WAL")
    held.execute("BEGIN")
    held.execute("SELECT COUNT(*) FROM source_search_jobs").fetchone()
    last = planned("wal-only")
    dispatch(sqlite_ledger, last, FixtureAdapter(fail=True))
    assert Path(str(source) + "-wal").stat().st_size > 0
    try:
        with sqlite3.connect(source) as connection:
            old_rows = {
                table: sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall())
                for table in TABLES
            }
        import_sqlite_ledger(database, source, recovery_generation=7, confirmed_quiesced=True)
        activate(database)
        restored = reopen(database)
        assert restored.identity == sqlite_ledger.identity
        with database.connect() as connection:
            new_rows = {
                table: sorted(
                    tuple(row) for row in connection.execute(text(f'SELECT * FROM "{table}"'))
                )
                for table in TABLES
            }
        assert new_rows == old_rows
        adapter = FixtureAdapter()
        assert dispatch(restored, planned(), adapter) == success
        assert dispatch(restored, last, adapter).status == "unknown"
        assert dispatch(restored, interrupted, adapter).status == "running"
        assert not adapter.calls
    finally:
        held.rollback()
        held.close()


def test_import_requires_quiescence_and_never_overwrites_existing_history(
    database, initialized, tmp_path
):
    from katcha.editorial.source_intelligence.postgres_jobs import (
        SearchLedgerRecoveryError,
        import_sqlite_ledger,
    )

    source = tmp_path / "other.sqlite"
    SearchJobLedger(source)
    dispatch(initialized, planned())
    with pytest.raises((SearchLedgerRecoveryError, ValueError)):
        import_sqlite_ledger(database, source, recovery_generation=7)
    with pytest.raises((SearchLedgerRecoveryError, ValueError)):
        import_sqlite_ledger(database, source, recovery_generation=7, confirmed_quiesced=True)
    assert initialized.read(planned().job_id, scope=SCOPE).status == "succeeded"
    with pytest.raises((SearchLedgerRecoveryError, FileNotFoundError, ValueError)):
        import_sqlite_ledger(
            database, tmp_path / "missing.sqlite", recovery_generation=7, confirmed_quiesced=True
        )
    assert not (tmp_path / "missing.sqlite").exists()


def test_quarantined_restore_blocks_even_same_generation_until_review(database, initialized):
    from katcha.editorial.source_intelligence.postgres_jobs import (
        SearchLedgerRecoveryError,
        inspect_postgres_ledger,
        quarantine_postgres_ledger,
    )

    prior = dispatch(initialized, planned())
    quarantine_postgres_ledger(database)
    assert inspect_postgres_ledger(database)["recovery_required"] is True
    assert reopen(database).read(planned().job_id, scope=SCOPE) == prior
    adapter = FixtureAdapter()
    with pytest.raises(SearchLedgerRecoveryError):
        dispatch(reopen(database), planned("uncertain-window"), adapter)
    assert not adapter.calls


def test_activation_rejects_stale_identity_and_seal(database, initialized):
    from katcha.editorial.source_intelligence.postgres_jobs import (
        SearchLedgerRecoveryError,
        activate_postgres_ledger,
        inspect_postgres_ledger,
        quarantine_postgres_ledger,
    )

    report = inspect_postgres_ledger(database)
    dispatch(initialized, planned())
    quarantine_postgres_ledger(database)
    with pytest.raises((SearchLedgerRecoveryError, ValueError)):
        activate_postgres_ledger(
            database,
            expected_identity=report["ledger_id"],
            expected_seal=report["integrity_sha256"],
            expected_generation=report["recovery_generation"],
            recovery_generation=7,
            completeness_evidence="Stale fixture review must be rejected",
            confirmed_quiesced=True,
        )
    current = inspect_postgres_ledger(database)
    with pytest.raises((SearchLedgerRecoveryError, ValueError)):
        activate_postgres_ledger(
            database,
            expected_identity="wrong-ledger",
            expected_seal=current["integrity_sha256"],
            expected_generation=current["recovery_generation"],
            recovery_generation=7,
            completeness_evidence="Wrong source history must be rejected",
            confirmed_quiesced=True,
        )
    assert inspect_postgres_ledger(database)["recovery_required"] is True


def test_invalid_sqlite_import_rolls_back_all_rows_and_can_be_repaired(database, tmp_path):
    from katcha.editorial.source_intelligence.postgres_jobs import (
        SearchLedgerRecoveryError,
        import_sqlite_ledger,
    )

    source = tmp_path / "malformed.sqlite"
    ledger = SearchJobLedger(source)
    dispatch(ledger, planned())
    with sqlite3.connect(source) as connection:
        valid = connection.execute("SELECT receipt FROM source_search_jobs").fetchone()[0]
        connection.execute("UPDATE source_search_jobs SET receipt='malformed historical receipt'")
    with pytest.raises((SearchLedgerRecoveryError, ValueError)):
        import_sqlite_ledger(database, source, recovery_generation=7, confirmed_quiesced=True)
    with database.connect() as connection:
        for table in TABLES:
            assert connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one() == 0
        assert (
            connection.execute(text("SELECT COUNT(*) FROM source_search_ledger_head")).scalar_one()
            == 0
        )
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE source_search_jobs SET receipt=?", (valid,))
    import_sqlite_ledger(database, source, recovery_generation=7, confirmed_quiesced=True)
    activate(database)
    assert reopen(database).read(planned().job_id, scope=SCOPE).status == "succeeded"


def _kill_during_finish(url, marker):
    from sqlalchemy import event

    engine = create_engine(url)

    @event.listens_for(engine, "after_cursor_execute")
    def crash_after_candidate_insert(
        connection, cursor, statement, parameters, context, executemany
    ):
        if statement.lstrip().startswith("INSERT INTO source_search_candidates"):
            os._exit(75)

    class ProcessAdapter(FixtureAdapter):
        def discover(self, query, cursor):
            Path(marker).write_text("fixture-call\n")
            return super().discover(query, cursor)

    dispatch(reopen(engine), planned(), ProcessAdapter())
    os._exit(76)


def test_process_death_mid_finish_rolls_back_candidate_receipt_event_and_seal(
    database, initialized, tmp_path
):
    marker = tmp_path / "finish-provider-call"
    process = multiprocessing.get_context("fork").Process(
        target=_kill_during_finish,
        args=(database.url.render_as_string(hide_password=False), str(marker)),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 75
    assert marker.read_text() == "fixture-call\n"
    restarted = reopen(database)
    assert restarted.read(planned().job_id, scope=SCOPE).status == "running"
    assert restarted.candidates(planned().job_id, scope=SCOPE) == ()
    assert [r.status for r in restarted.history(planned().job_id, scope=SCOPE)] == ["running"]
    adapter = FixtureAdapter()
    assert dispatch(restarted, planned(), adapter).status == "running"
    assert restarted.mark_interrupted(planned().job_id, scope=SCOPE).status == "unknown"
    assert dispatch(restarted, planned(), adapter).status == "unknown"
    assert not adapter.calls


def test_import_mid_insert_failure_rolls_back_previous_tables_and_head(database, tmp_path):
    from sqlalchemy import event

    from katcha.editorial.source_intelligence.postgres_jobs import import_sqlite_ledger

    source = tmp_path / "transaction.sqlite"
    dispatch(SearchJobLedger(source), planned())

    def interrupt(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO source_search_receipt_events"):
            raise RuntimeError("Injected transactional import interruption")

    event.listen(database, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="transactional import interruption"):
            import_sqlite_ledger(database, source, recovery_generation=7, confirmed_quiesced=True)
    finally:
        event.remove(database, "before_cursor_execute", interrupt)
    with database.connect() as connection:
        for table in (*TABLES, "source_search_ledger_head"):
            assert connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one() == 0
    import_sqlite_ledger(database, source, recovery_generation=7, confirmed_quiesced=True)
    activate(database)
    assert reopen(database).read(planned().job_id, scope=SCOPE).status == "succeeded"


@pytest.mark.parametrize(
    "delete_statement",
    [
        "DELETE FROM source_search_budgets",
        "DELETE FROM source_search_candidates",
        "DELETE FROM source_search_receipt_events WHERE event_id=1",
    ],
)
def test_partial_historical_sqlite_cannot_be_certified_complete(
    database, tmp_path, delete_statement
):
    from katcha.editorial.source_intelligence.postgres_jobs import (
        SearchLedgerRecoveryError,
        import_sqlite_ledger,
    )

    source = tmp_path / "partial.sqlite"
    dispatch(SearchJobLedger(source), planned())
    with sqlite3.connect(source) as connection:
        connection.execute(delete_statement)
    with pytest.raises(SearchLedgerRecoveryError):
        import_sqlite_ledger(database, source, recovery_generation=7, confirmed_quiesced=True)
    with database.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM source_search_ledger_head")).scalar_one()
            == 0
        )


def test_imported_rate_limit_retry_is_atomic_bounded_and_replayable(
    database, tmp_path, monkeypatch
):
    from datetime import datetime, timedelta

    from katcha.acquisition.adapters import DiscoveryProviderError
    from katcha.editorial.source_intelligence.postgres_jobs import import_sqlite_ledger

    policy = ProviderPolicyV1(version="fixture-policy-v1", max_attempts=2)
    job = planned(policy=policy)
    source = tmp_path / "rate-limit.sqlite"

    class RateLimited(FixtureAdapter):
        def discover(self, query, cursor):
            raise DiscoveryProviderError(
                "Offline rate limit", kind="rate_limited", status_code=429, retry_after_seconds=1
            )

    prior = dispatch(SearchJobLedger(source), job, RateLimited(), policy)
    assert prior.status == "rate_limited"
    import_sqlite_ledger(database, source, recovery_generation=7, confirmed_quiesced=True)
    activate(database)
    monkeypatch.setattr(
        "katcha.editorial.source_intelligence.jobs._now",
        lambda: datetime.fromisoformat(prior.finished_at) + timedelta(seconds=2),
    )
    calls = []

    def retry(_):
        return reopen(database).dispatch(
            job,
            scope=SCOPE,
            policy=policy,
            adapter=FixtureAdapter(calls),
            capability=CAP,
            retry_rate_limit=True,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(retry, range(8)))
    assert len(calls) == 1
    assert {r.status for r in results} <= {"running", "succeeded"}
    assert reopen(database).read(job.job_id, scope=SCOPE).attempt == 2
    assert [r.status for r in reopen(database).history(job.job_id, scope=SCOPE)] == [
        "running",
        "rate_limited",
        "running",
        "succeeded",
    ]
    assert retry(None).status == "succeeded"
    assert len(calls) == 1


def test_lost_postgres_schema_never_recreates_evidence(database, initialized):
    from katcha.editorial.source_intelligence.postgres_jobs import SearchLedgerRecoveryError

    dispatch(initialized, planned())
    with database.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    adapter = FixtureAdapter()
    with pytest.raises(SearchLedgerRecoveryError, match="unavailable or incomplete"):
        dispatch(reopen(database), planned(), adapter)
    assert not adapter.calls
    with database.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'")
            ).scalar_one()
            == 0
        )


def test_stale_dump_then_canonical_restore_quarantine_blocks_same_generation(
    database, initialized, tmp_path
):
    from katcha.editorial.source_intelligence.postgres_jobs import (
        SearchLedgerRecoveryError,
        inspect_postgres_ledger,
    )

    dispatch(initialized, planned("before-backup"))
    dump = tmp_path / "stale.dump"
    env, args = dump_restore(database, dump)
    missing = planned("unknown-after-backup")
    dispatch(initialized, missing, FixtureAdapter(fail=True))
    restore(database, dump, env, args)
    # Execute the exact inhibition SQL shipped in the canonical restore script.
    script = (
        Path(__file__).resolve().parents[1] / "deploy/scripts/postgres-disaster-restore.sh"
    ).read_text()
    quarantine_sql = script.split("<<'SQL'\n", 1)[1].split("\nSQL\n", 1)[0]
    with database.begin() as connection:
        connection.execute(text("SET LOCAL synchronous_commit = 'off'"))
        connection.execute(text(quarantine_sql))
        assert connection.scalar(text("SHOW synchronous_commit")) == "on"
    assert inspect_postgres_ledger(database)["recovery_required"] is True
    restored = reopen(database, generation=7)
    assert restored.read(missing.job_id, scope=SCOPE) is None
    calls = FixtureAdapter()
    with pytest.raises(SearchLedgerRecoveryError, match="offline recovery review"):
        dispatch(restored, missing, calls)
    assert not calls.calls


def test_integrity_scan_capacity_rolls_back_claim_before_provider(database, initialized):
    from katcha.editorial.source_intelligence.postgres_jobs import (
        PostgresSearchJobLedger,
        SearchLedgerRecoveryError,
        inspect_postgres_ledger,
    )

    before = inspect_postgres_ledger(database)
    bounded = PostgresSearchJobLedger(
        database, recovery_generation=7, dispatch_enabled=True, max_evidence_rows=1
    )
    adapter = FixtureAdapter()
    with pytest.raises(SearchLedgerRecoveryError, match="scan bound"):
        dispatch(bounded, planned(), adapter)
    assert not adapter.calls
    assert inspect_postgres_ledger(database) == before


def test_upgrade_old_postgres_schema_keeps_evidence_uninitialized(database):
    import sys

    from katcha.editorial.source_intelligence.postgres_jobs import SearchLedgerRecoveryError

    database.dispose()
    with database.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    env = {
        **os.environ,
        "KATCHA_DATABASE_URL": database.url.render_as_string(hide_password=False),
    }
    for revision in ("0062_requirement_shot_matching", "head"):
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            check=True,
            capture_output=True,
        )
    with database.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0063_authoritative_search_ledger"
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM source_search_ledger_head")).scalar_one()
            == 0
        )
    adapter = FixtureAdapter()
    with pytest.raises(SearchLedgerRecoveryError, match="uninitialized"):
        dispatch(reopen(database), planned(), adapter)
    assert not adapter.calls


def test_ledger_overrides_async_pooled_session_before_durable_claim(database, initialized):
    from katcha.editorial.source_intelligence.postgres_jobs import PostgresSearchJobLedger

    weaker = create_engine(database.url, connect_args={"options": "-c synchronous_commit=off"})
    try:
        with weaker.connect() as connection:
            assert connection.scalar(text("SHOW synchronous_commit")) == "off"
        ledger = PostgresSearchJobLedger(weaker, recovery_generation=7, dispatch_enabled=True)
        with ledger._db() as adapter:
            assert adapter.connection.scalar(text("SHOW synchronous_commit")) == "on"
        assert dispatch(ledger, planned()).status == "succeeded"
        with weaker.connect() as connection:
            assert connection.scalar(text("SHOW synchronous_commit")) == "off"
        assert reopen(database).read(planned().job_id, scope=SCOPE).status == "succeeded"
    finally:
        weaker.dispose()
