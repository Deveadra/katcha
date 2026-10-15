from temporalio import activity

from katcha.services.production_meme_assets import prepare_production_memes


@activity.defn
def prepare_meme_assets(production_id: str) -> dict:
    return prepare_production_memes(production_id)
