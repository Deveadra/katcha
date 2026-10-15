from fastapi import APIRouter, HTTPException, Request

from katcha.api.control_auth import control_actor, control_allowed_channel_ids, control_scopes
from katcha.config import get_settings
from katcha.security.secrets import SecretConfigurationError
from katcha.services.application_ai_settings import masked_settings, save_settings

router = APIRouter(tags=["AI settings"])


def _require_global_operator(request: Request) -> None:
    if "*" not in control_scopes(request) or control_allowed_channel_ids(request) is not None:
        raise HTTPException(403, "A global operator is required to manage shared AI settings")


@router.get("/v1/integrations/ai/settings")
def ai_settings(request: Request) -> dict:
    _require_global_operator(request)
    return masked_settings(get_settings())


@router.post("/v1/integrations/ai/settings")
def update_ai_settings(changes: dict[str, str], request: Request) -> dict:
    _require_global_operator(request)
    try:
        return save_settings(changes, get_settings(), actor=control_actor(request))
    except (ValueError, SecretConfigurationError) as exc:
        raise HTTPException(400, detail=str(exc)) from None
