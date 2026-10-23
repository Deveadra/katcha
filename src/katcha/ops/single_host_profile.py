"""Strict opt-in selection for the supervised single-host Compose profile."""
import sys
from pathlib import Path


def enabled_from_file(path: Path) -> bool:
    values = []
    for line in path.read_text().splitlines():
        key, separator, value = line.strip().partition("=")
        if key.strip() == "KATCHA_SINGLE_HOST_PROFILE_ENABLED":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if not separator or value not in {"true", "false"}:
                raise ValueError("Single-host profile requires explicit true or false")
            values.append(value)
    if len(values) > 1:
        raise ValueError("Duplicate single-host profile selection")
    return values == ["true"]


if __name__ == "__main__":
    print("true" if enabled_from_file(Path(sys.argv[1])) else "false")
