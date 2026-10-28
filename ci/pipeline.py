"""Real Actions delivery for the isolated three-service voting application."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

LOG = logging.getLogger("voting-delivery")
OWNER = "boardflow-lab-lenshood"
ROOT = Path("/home/lenshood/boardflow-real-uat/deployments")
LABEL = "aiworkforce.real-uat.operation"
SERVICES = {"vote", "result", "worker"}


def run(*args: str) -> str:
    completed = subprocess.run(args, capture_output=True, text=True, timeout=900)
    if completed.returncode:
        # No command arguments or environment values in error messages.
        if args[:2] == ("docker", "build"):
            # Build contexts contain public source only, with no injected credentials.
            LOG.error("Public-source build diagnostics: %s", completed.stderr[-12000:])
        raise RuntimeError(f"{args[0]} failed with exit code {completed.returncode}")
    return completed.stdout.strip()


def docker(*args: str) -> str:
    return run("docker", *args)


def inspect(container: str) -> dict:
    return json.loads(docker("inspect", container))[0]


def wait_http(url: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1)
    raise RuntimeError("A deployed web service did not become healthy")


def build_images(target: dict, scope: str) -> dict[str, str]:
    images = {}
    for revision in target["repositories"]:
        name, sha = revision["repository_id"], revision["sha"]
        scratch = Path(tempfile.mkdtemp(prefix="boardflow-source-"))
        try:
            run("git", "clone", "--no-checkout", f"https://github.com/{OWNER}/{name}.git", str(scratch / "source"))
            source = scratch / "source"
            run("git", "-C", str(source), "checkout", "--detach", sha)
            if run("git", "-C", str(source), "rev-parse", "HEAD") != sha:
                raise RuntimeError("Checkout differs from the frozen target")
            tag = f"boardflow-real-uat/{name}:{sha}"
            cached = subprocess.run(["docker", "image", "inspect", tag], capture_output=True, text=True, timeout=30)
            if cached.returncode == 0:
                image = json.loads(cached.stdout)[0]
                if image["Config"]["Labels"].get("org.opencontainers.image.revision") != sha:
                    raise RuntimeError("Cached image has a different source revision")
                images[name] = image["Id"]
                LOG.info("Reusing exact built service=%s sha=%s image=%s", name, sha, image["Id"])
                continue
            tree = run("git", "-C", str(source), "rev-parse", "HEAD^{tree}")
            main_sha = run("git", "-C", str(source), "rev-parse", "origin/main")
            main_tree = run("git", "-C", str(source), "rev-parse", "origin/main^{tree}")
            tree_tag = f"boardflow-real-uat/{name}:tree-{tree}"
            equivalent_tag = tree_tag
            equivalent = subprocess.run(["docker", "image", "inspect", equivalent_tag], capture_output=True, text=True, timeout=30)
            if equivalent.returncode and tree == main_tree:
                equivalent_tag = f"boardflow-real-uat/{name}:{main_sha}"
                equivalent = subprocess.run(["docker", "image", "inspect", equivalent_tag], capture_output=True, text=True, timeout=30)
            if equivalent.returncode == 0:
                # The source tree is identical. Preserve the built filesystem,
                # with a new immutable image carrying this candidate commit.
                base_image = json.loads(equivalent.stdout)[0]
                label = base_image["Config"]["Labels"]
                if label.get("org.opencontainers.image.source_tree") == tree or (tree == main_tree and label.get("org.opencontainers.image.revision") == main_sha):
                    recipe = scratch / "identity-layer"
                    recipe.mkdir()
                    (recipe / "Dockerfile").write_text(f"FROM {equivalent_tag}\n")
                    docker("build", "--pull=false", "--label", f"org.opencontainers.image.revision={sha}", "--label", f"org.opencontainers.image.source_tree={tree}", "--tag", tag, str(recipe))
                    docker("tag", tag, tree_tag)
                    images[name] = json.loads(docker("image", "inspect", tag))[0]["Id"]
                    continue
            LOG.info("Building service=%s sha=%s", name, sha)
            docker("build", "--network=host", "--label", f"org.opencontainers.image.revision={sha}",
                "--label", f"org.opencontainers.image.source_tree={tree}",
                "--label", f"aiworkforce.real-uat.repository={name}", "--tag", tag, str(source))
            images[name] = json.loads(docker("image", "inspect", tag))[0]["Id"]
            docker("tag", tag, tree_tag)
        finally:
            shutil.rmtree(scratch)
    return images


def assert_owned(name: str, operation: str) -> dict:
    value = inspect(name)
    if value["Config"]["Labels"].get(LABEL) != operation:
        raise RuntimeError("Resource ownership does not match the original operation")
    return value


def observe(target: dict, operation: str, images: dict) -> dict:
    prefix = f"bfgh-{operation}"
    revisions = {item["repository_id"]: item["sha"] for item in target["repositories"]}
    actual = {"environment_id": target["environment_id"], "config_revision": target["config_revision"],
        "repositories": target["repositories"], "services": [], "endpoints": {}}
    for name in sorted(SERVICES):
        container = assert_owned(f"{prefix}-{name}", operation)
        image = json.loads(docker("image", "inspect", container["Image"]))[0]
        if container["Image"] != images[name] or image["Config"]["Labels"].get("org.opencontainers.image.revision") != revisions[name]:
            raise RuntimeError("Deployed image or source SHA differs from the target")
        if not container["State"]["Running"]:
            raise RuntimeError("Deployed service has stopped")
        if name in ("vote", "result"):
            port = container["NetworkSettings"]["Ports"]["80/tcp"][0]["HostPort"]
            url = f"http://172.16.28.129:{port}"
            wait_http(url)
            actual["endpoints"][name] = url
        actual["services"].append({"service_id": name, "status": "SUCCEEDED", "healthy": True,
            "artifact_ref": container["Image"], "repositories": [{"repository_id": name, "sha": revisions[name]}]})
    actual["environment_url"] = actual["endpoints"]["vote"]
    for key in ("release_target_ref", "version_set_ref"):
        if key in target:
            actual[key] = target[key]
    return actual


def deploy(target: dict, operation: str, images: dict) -> dict:
    prefix = f"bfgh-{operation}"
    docker("network", "create", "--label", f"{LABEL}={operation}", prefix)
    password = secrets.token_urlsafe(24)
    shared = ["--detach", "--init", "--network", prefix, "--label", f"{LABEL}={operation}"]
    docker("run", *shared, "--name", f"{prefix}-redis", "--network-alias", "redis", "redis:7-alpine")
    docker("run", *shared, "--name", f"{prefix}-db", "--network-alias", "db",
        "--tmpfs", "/var/lib/postgresql/data", "--env", f"POSTGRES_PASSWORD={password}", "postgres:15-alpine")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = subprocess.run(["docker", "exec", f"{prefix}-db", "pg_isready", "-U", "postgres"], capture_output=True)
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Isolated PostgreSQL did not become ready")
    for name in ("worker", "vote", "result"):
        extra = []
        if name == "worker":
            extra = ["--env", f"DATABASE_CONNECTION_STRING=Server=db;Username=postgres;Password={password};"]
        if name == "result":
            extra = ["--env", f"DATABASE_URL=postgres://postgres:{password}@db/postgres"]
        if name in ("vote", "result"):
            extra += ["--publish", "0:80"]
        docker("run", *shared, "--name", f"{prefix}-{name}", "--network-alias", name, *extra, images[name])
    return observe(target, operation, images)


def settle(target: dict) -> dict:
    original = target["settles_operation"]
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", original):
        raise RuntimeError("Invalid original operation")
    saved_path = ROOT / f"{original}.json"
    saved = json.loads(saved_path.read_text()) if saved_path.exists() else None
    if saved and saved["target"]["environment_id"] != target["environment_id"]:
        raise RuntimeError("Settlement refers to another environment")
    ids = docker("ps", "--all", "--quiet", "--filter", f"label={LABEL}={original}").split()
    networks = docker("network", "ls", "--quiet", "--filter", f"label={LABEL}={original}").split()
    if saved is None and (ids or networks):
        raise RuntimeError("Resources exist without a deployment journal; manual investigation required")
    for identity in ids:
        assert_owned(identity, original)
    if ids:
        docker("rm", "--force", *ids)
    for network in networks:
        state = json.loads(docker("network", "inspect", network))[0]
        if state["Labels"].get(LABEL) != original or state.get("Containers"):
            raise RuntimeError("Original network still contains other resources")
        docker("network", "rm", network)
    return {"environment_id": target["environment_id"], "config_revision": target["config_revision"],
        "settles_operation": original, "settles_run": target["settles_run"], "settled": True,
        "services": [{"service_id": name, "disposition": "REMOVED", "active": False, "settled": True} for name in sorted(SERVICES)]}


def main():
    operation = os.environ["BOARD_DELIVERY_OPERATION"]
    target = json.loads(os.environ["BOARD_DELIVERY_TARGET"])
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", operation) or target["environment_id"] not in ("real-uat-test", "real-uat-prod"):
        raise RuntimeError("Operation outside the isolated UAT scope")
    if {item["repository_id"] for item in target["repositories"]} != SERVICES or any(not re.fullmatch(r"[a-f0-9]{40}", item["sha"]) for item in target["repositories"]):
        raise RuntimeError("Invalid frozen repository set")
    ROOT.mkdir(parents=True, exist_ok=True)
    action = target.get("action", "DEPLOY_CANDIDATE")
    if action == "SETTLE":
        actual = settle(target)
    elif action == "VERIFY":
        original = target["verifies_operation"]
        saved = json.loads((ROOT / f"{original}.json").read_text())
        images = {item["service_id"]: item["artifact_ref"] for item in target["artifacts"]}
        if saved["target"].get("action") != "DEPLOY_PROD":
            raise RuntimeError("Verification must refer to an actual production deployment")
        actual = observe(target, original, images)
        actual.update(verified=True, verifies_operation=original, verifies_run=target["verifies_run"])
    elif action in ("DEPLOY_CANDIDATE", "DEPLOY_PROD"):
        (ROOT / f"{operation}.json").write_text(json.dumps({"target": target, "stage": "building"}))
        images = build_images(target, operation) if action == "DEPLOY_CANDIDATE" else {item["service_id"]: item["artifact_ref"] for item in target["artifacts"]}
        # Journal the scope before creating resources, including partial failures.
        (ROOT / f"{operation}.json").write_text(json.dumps({"target": target, "images": images}))
        actual = deploy(target, operation, images)
    else:
        raise RuntimeError("Unsupported isolated pipeline action")
    value = {"operation_id": operation, "deployment": actual}
    Path("observation.json").write_text(json.dumps(value, indent=2))
    (ROOT / f"{operation}.json").write_text(json.dumps({"target": target, "observation": value}))
    LOG.info("Completed operation=%s action=%s", operation, action)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
