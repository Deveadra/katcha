"""Authoritative search evidence schema; creating tables does not initialize evidence."""

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, Integer, Table, Text

from katcha.db import Base

SOURCE_LEDGER_COLUMNS = {
    "source_search_ledger_identity": ("singleton", "ledger_id"),
    "source_search_budgets": ("scope_key", "provider", "policy", "calls", "results", "cost"),
    "source_search_jobs": ("job_id", "scope_key", "intent", "receipt"),
    "source_search_candidates": ("job_id", "candidate_id", "scope_key", "payload"),
    "source_search_receipt_events": ("event_id", "scope_key", "job_id", "receipt"),
}

search_ledger_head = Table(
    "source_search_ledger_head",
    Base.metadata,
    Column("singleton", Integer, primary_key=True),
    Column("ledger_id", Text, nullable=False),
    Column("recovery_generation", BigInteger, nullable=False),
    Column("integrity_sha256", Text, nullable=False),
    Column("recovery_required", Boolean, nullable=False),
    Column("activation_evidence", Text, nullable=False),
    CheckConstraint("singleton = 1", name="ck_search_ledger_head_singleton"),
    CheckConstraint("recovery_generation > 0", name="ck_search_ledger_head_epoch"),
)
search_ledger_identity = Table(
    "source_search_ledger_identity",
    Base.metadata,
    Column("singleton", Integer, primary_key=True),
    Column("ledger_id", Text, nullable=False),
    CheckConstraint("singleton = 1", name="ck_search_ledger_identity_singleton"),
)
search_ledger_budgets = Table(
    "source_search_budgets",
    Base.metadata,
    Column("scope_key", Text, primary_key=True),
    Column("provider", Text, primary_key=True),
    Column("policy", Text, nullable=False),
    Column("calls", BigInteger, nullable=False),
    Column("results", BigInteger, nullable=False),
    Column("cost", BigInteger, nullable=False),
    CheckConstraint(
        "calls >= 0 AND results >= 0 AND cost >= 0", name="ck_search_budget_nonnegative"
    ),
)
search_ledger_jobs = Table(
    "source_search_jobs",
    Base.metadata,
    Column("job_id", Text, primary_key=True),
    Column("scope_key", Text, nullable=False),
    Column("intent", Text, nullable=False),
    Column("receipt", Text, nullable=False),
)
search_ledger_candidates = Table(
    "source_search_candidates",
    Base.metadata,
    Column("job_id", Text, primary_key=True),
    Column("candidate_id", Text, primary_key=True),
    Column("scope_key", Text, nullable=False),
    Column("payload", Text, nullable=False),
)
search_ledger_events = Table(
    "source_search_receipt_events",
    Base.metadata,
    # IDs allocated under the head lock, preserving SQLite's max-ID + 1 semantics.
    Column("event_id", BigInteger, primary_key=True, autoincrement=False),
    Column("scope_key", Text, nullable=False),
    Column("job_id", Text, nullable=False),
    Column("receipt", Text, nullable=False),
)
