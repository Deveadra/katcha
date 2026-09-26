from __future__ import annotations

import re
from pathlib import Path


def test_temporal_workflows_do_not_request_object_typed_activity_results() -> None:
    orchestration = Path("src/katcha/orchestration")
    offenders: list[str] = []
    pattern = re.compile(r"result_type\s*=\s*dict\[str,\s*object\]")

    for path in sorted(orchestration.glob("*_workflows.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))

    assert not offenders, (
        "Temporal cannot deserialize activity payloads using dict[str, object]; "
        f"remove that explicit result_type from: {offenders}"
    )
