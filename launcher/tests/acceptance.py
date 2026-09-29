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


def wait_for_workspace(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            snapshot = json.loads(request("/runtime/status"))
        except OSError:
            time.sleep(1)
            continue
        if snapshot.get("workspace_ready"):
            return snapshot
        if snapshot["phase"] == "failed":
            raise RuntimeError(json.dumps(snapshot["events"][-20:], indent=2))
        time.sleep(1)
    raise TimeoutError("Katcha workspace did not become ready")


if __name__ == "__main__":
    process = subprocess.Popen(["python3", "launcher/runtime.py", "--no-browser"])
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                request("/runtime/start", {})
                break
            except (OSError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        assert b"What needs you now." in request("/home/assets/home.html")
        assert b"Production" in request("/editing/assets/editing.html")
        assert b'data-katcha-page="clips"' in request("/editing/assets/clips.html")
        assert b"Channel Studio" in request("/channels/assets/channels.html")
        assert b"Katcha AI" in request("/ai/assets/ai.html")
        cold_started = time.monotonic()
        workspace_state = wait_for_workspace(1800)
        print(
            f"Initial workspace readiness: {time.monotonic() - cold_started:.2f}s",
            flush=True,
        )
        assert workspace_state["workspace_ready"]
        state = wait_for("ready", 1800)
        print(f"Initial full readiness: {time.monotonic() - cold_started:.2f}s", flush=True)
        services = {row["Service"] for row in state["services"]}
        assert {
            "postgres",
            "temporal",
            "minio",
            "api",
            "worker",
            "analysis-worker",
            "renderer",
            "production-worker",
            "telegram-worker",
            "intelligence-worker",
        } <= services
        assert {
            "publishing-worker",
            "longform-worker",
            "discovery-worker",
            "trends-worker",
        }.isdisjoint(services)
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
        # Measure a real warm restart and ensure that it does not rebuild images.
        warm_started = time.monotonic()
        request("/runtime/start", {})
        workspace_state = wait_for_workspace(120)
        print(
            f"Warm workspace readiness: {time.monotonic() - warm_started:.2f}s",
            flush=True,
        )
        assert workspace_state["workspace_ready"]
        state = wait_for("ready", 420)
        assert state["desired_running"]
        # Status retains only 300 events; busy worker logs can evict the build event.
        journal = [json.loads(line) for line in request("/runtime/diagnostics").splitlines()]
        last_start = max(
            index for index, event in enumerate(journal)
            if event["component"] == "launcher" and event["message"] == "start requested"
        )
        messages = [event["message"] for event in journal[last_start:]]
        assert "Reusing unchanged workspace image." in messages
        assert "Reusing unchanged application images." in messages
        assert "Preparing the lightweight workspace control plane locally." not in messages
        assert "Preparing new or changed application images locally." not in messages
        print(f"Warm full readiness: {time.monotonic() - warm_started:.2f}s", flush=True)
        request("/runtime/stop", {})
        state = wait_for("stopped", 120)
        assert not state["desired_running"]
        assert subprocess.check_output(volume_args, text=True) == before
        print("Full startup, warm restart, authenticated GUI/API, and safe stop passed.")
    finally:
        process.terminate()
        process.wait(timeout=10)
