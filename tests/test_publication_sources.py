from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.publishing_models import Publication


def test_publication_requires_exactly_one_source_contract() -> None:
    table = Publication.__table__
    assert table.c.production_id.nullable is True
    assert table.c.compilation_id.nullable is True

    checks = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_publications_exactly_one_source" in checks


def test_publication_has_unique_source_channel_constraints() -> None:
    table = Publication.__table__
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("production_id", "youtube_connection_id") in unique_columns
    assert ("compilation_id", "youtube_connection_id") in unique_columns
