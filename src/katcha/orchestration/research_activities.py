from datetime import datetime

from temporalio import activity

from katcha.services.research import prepare_research_jobs


@activity.defn
def prepare_research_jobs_activity(now: str) -> list[dict]:
    return prepare_research_jobs(datetime.fromisoformat(now))
