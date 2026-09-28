from katcha.api.main import app
from katcha.services.external_edit import _invideo_instructions


def test_external_provider_routes_are_registered() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/integrations/providers",
        "/v1/integrations/invideo/handoffs",
        "/v1/integrations/invideo/handoffs/{handoff_id}",
        "/v1/integrations/invideo/handoffs/{handoff_id}/manifest",
        "/v1/integrations/invideo/handoffs/{handoff_id}/package",
        "/v1/integrations/invideo/handoffs/{handoff_id}/output",
        "/v1/integrations/invideo/handoffs/{handoff_id}/adopt",
    }
    assert expected <= set(paths)
    assert "post" in paths["/v1/integrations/invideo/handoffs"]
    assert "post" in paths["/v1/integrations/invideo/handoffs/{handoff_id}/output"]
    assert "post" in paths["/v1/integrations/invideo/handoffs/{handoff_id}/adopt"]


def test_invideo_brief_keeps_katcha_as_source_of_truth() -> None:
    instructions = _invideo_instructions(
        brand={"brand_key": "ranksnaxx"},
        blueprint={"key": "persona_commentary"},
    )
    text = " ".join(instructions).lower()
    assert "source of truth" in text
    assert "do not publish directly" in text
    assert "brand.json" in text
    assert "editing-recipe.json" in text
