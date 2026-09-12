import argparse
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def compose(*args, **kwargs):
    root = ROOT / ".local/data" / os.environ.get("EPHIOS_STACK", "test")
    if not root.resolve().is_relative_to(ROOT):
        raise SystemExit("Container data must stay in the workspace.")
    for name in ("ephios", "postgres", "redis", "mail"):
        directory = root / name
        if not directory.resolve().is_relative_to(ROOT):
            raise SystemExit("Container data must stay in the workspace.")
        directory.mkdir(parents=True, exist_ok=True)
    project = "shift-coordination-" + hashlib.sha256(str(root).encode()).hexdigest()[:10]
    return run(
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        "deploy/compose.yaml",
        *args,
        env={**os.environ, "EPHIOS_DATA_ROOT": str(root)},
        **kwargs,
    )


def build():
    run("uv", "build", "--wheel", "--sdist")
    context = ROOT / ".local/build"
    context.mkdir(parents=True, exist_ok=True)
    allowed = {"Dockerfile"}
    for wheel in (ROOT / "dist").glob("ephios_shift_coordination_plugin-*.whl"):
        shutil.copy2(wheel, context / wheel.name)
        allowed.add(wheel.name)
    for path in context.iterdir():
        if path.name not in allowed:
            raise SystemExit(f"Unexpected build context entry: {path.name}")
    shutil.copy2(ROOT / "deploy/Dockerfile", context / "Dockerfile")


def up():
    build()
    compose("up", "--build", "--wait", "--wait-timeout", "300")
    print(f"Local ephios: http://127.0.0.1:{os.environ.get('EPHIOS_HTTP_PORT', '8097')}")


def e2e():
    try:
        up()
        compose(
            "exec", "-T", "app", "ephios", "shell", "-c", (ROOT / "tests/e2e/seed.py").read_text()
        )
        run("uv", "run", "--locked", "pytest", "-m", "e2e", "tests/e2e", "-q")
        compose("restart", "app")
        compose("up", "--wait", "--wait-timeout", "180")
        compose(
            "exec",
            "-T",
            "app",
            "ephios",
            "shell",
            "-c",
            "from importlib.metadata import version; "
            "assert version('ephios-shift-coordination-plugin') == '0.1.0'; "
            "from ephios.core.plugins import get_enabled_plugins; "
            "assert any(p.module == 'ephios_shift_coordination' for p in get_enabled_plugins())",
        )
    finally:
        try:
            with (ROOT / ".local/test-results/containers.log").open("w") as log:
                compose("logs", "--no-color", stdout=log, stderr=subprocess.STDOUT)
        finally:
            compose("down")


def check():
    run("uv", "run", "--locked", "ruff", "check", ".")
    run("uv", "run", "--locked", "ruff", "format", "--check", ".")
    run("uv", "run", "--locked", "coverage", "run", "-m", "pytest", "-q")
    run("uv", "run", "--locked", "coverage", "report")
    run("uv", "run", "--locked", "coverage", "xml")
    run(
        "uv",
        "run",
        "--locked",
        "python",
        "-m",
        "django",
        "makemigrations",
        "ephios_shift_coordination",
        "--check",
        "--dry-run",
        "--settings=tests.settings",
    )
    e2e()


def setup():
    run("uv", "sync", "--locked")
    run("uv", "run", "--locked", "playwright", "install", "chromium", "--only-shell")
    run("git", "config", "--local", "core.hooksPath", ".githooks")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("setup", "build", "up", "down", "e2e", "check"))
    command = parser.parse_args().command
    actions = {
        "setup": setup,
        "build": build,
        "up": up,
        "down": lambda: compose("down"),
        "e2e": e2e,
        "check": check,
    }
    actions[command]()
