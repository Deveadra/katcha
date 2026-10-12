"""Operator intent for bounded, reference-first project media preparation."""

from pydantic import Field

from katcha.editorial.project_schemas import Contract, RequestKey


class StartMediaPreparation(Contract):
    idempotency_key: RequestKey
    expected_revision: int = Field(ge=0, strict=True)
    external_search_authorized: bool = False


class ReconcileMediaPreparation(Contract):
    confirmed_worker_stopped: bool = False
