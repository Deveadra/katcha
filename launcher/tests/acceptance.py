"""Full-stack acceptance for a disposable CI checkout with Docker, no provider secrets."""

import http.client
import json
import subprocess
import time


def request(path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=10)
    try:
        connection.request(
            "POST" if body is not None else "GET",
            path,
            json.dumps(body) if body is not None else None,
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = response.read()
        if response.status >= 400:
            raise RuntimeError(f"{path}: HTTP {response.status}")
        return payload
    finally:
        connection.close()


def wait_for(target, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            snapshot = json.loads(request("/runtime/status"))
        except OSError:
            time.sleep(2)
            continue
        if snapshot["phase"] == target:
            return snapshot
        if snapshot["phase"] == "failed":
            raise RuntimeError(json.dumps(snapshot["events"][-20:], indent=2))
        time.sleep(2)
    raise TimeoutError(f"Katcha did not reach {target}")


if __name__ == "__main__":
    process = subprocess.Popen(["python3", "launcher/runtime.py", "--no-browser"])
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                request("/runtime/start", {})
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        state = wait_for("ready", 1800)
        assert len(state["services"]) >= 15
        assert b"Editing control center" in request("/editing/assets/editing.html")
        assert isinstance(json.loads(request("/v1/channels")), list)
        volume_args = [
            "docker",
            "volume",
            "ls",
            "--filter",
            "label=com.docker.compose.project=katcha",
            "--format",
            "{{.Name}}",
        ]
        before = subprocess.check_output(volume_args, text=True)
        assert "postgres-data" in before and "minio-data" in before
        request("/runtime/stop", {})
        wait_for("stopped", 120)
        assert subprocess.check_output(volume_args, text=True) == before
        print("Full application startup, authenticated GUI/API, and non-destructive stop passed.")
    finally:
        process.terminate()
        process.wait(timeout=10)
