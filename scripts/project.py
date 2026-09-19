import argparse
import hashlib
import os
import shutil
import subprocess
import tomllib
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def compose(*args, **kwargs):
    root = ROOT / ".local/data" / os.environ.get("EPHIOS_STACK", "development")
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


def declared_version():
    """What this checkout says it is, so a version bump needs no second place to change."""
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def build():
    # Both directories are emptied first: a wheel left over from an earlier version would
    # otherwise travel into the image context and into the release artefacts.
    shutil.rmtree(ROOT / "dist", ignore_errors=True)
    run("uv", "build", "--wheel", "--sdist")
    context = ROOT / ".local/build"
    shutil.rmtree(context, ignore_errors=True)
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


def import_demo(no_admin=False, reset=False):
    if reset:
        print("Deleting the development stack data before importing the demo.")
        reset_data(os.environ.get("EPHIOS_STACK", "development"))
    up()
    compose(
        "exec",
        "-T",
        "app",
        "ephios",
        "shell",
        "-c",
        (ROOT / "scripts/demo.py").read_text() + "\nseed_demo()",
    )
    print("Demo members, the Dienst template and four planning periods are available.")
    if not no_admin:
        compose("exec", "app", "ephios", "createsuperuser")


@contextmanager
def e2e_environment():
    values = {
        "EPHIOS_STACK": "test",
        "EPHIOS_HTTP_PORT": os.environ.get("EPHIOS_TEST_HTTP_PORT", "8099"),
        "EPHIOS_MAIL_PORT": os.environ.get("EPHIOS_TEST_MAIL_PORT", "8100"),
        "EPHIOS_DATABASE_PORT": os.environ.get("EPHIOS_TEST_DATABASE_PORT", "5499"),
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def e2e():
    with e2e_environment():
        run_e2e()


def reset_data(stack):
    """Throw away a stack's database and files so the next start begins empty."""
    if os.environ.get("EPHIOS_STACK", "development") != stack:
        raise SystemExit("Refusing to reset a stack that is not selected.")
    root = ROOT / ".local/data" / stack
    if not root.resolve().is_relative_to(ROOT) or root.resolve() == ROOT.resolve():
        raise SystemExit("Container data must stay in the workspace.")
    compose("down", "--volumes")
    shutil.rmtree(root, ignore_errors=True)


def reset_test_data():
    """Kept data from earlier runs distorts every measurement, so each run starts empty."""
    reset_data("test")


def run_e2e():
    try:
        reset_test_data()
        up()
        compose(
            "exec",
            "-T",
            "app",
            "ephios",
            "shell",
            "-c",
            (ROOT / "scripts/demo.py").read_text() + "\nseed_demo()",
        )
        compose(
            "exec", "-T", "app", "ephios", "shell", "-c", (ROOT / "tests/e2e/seed.py").read_text()
        )
        run(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-m",
            "postgres",
            "tests/test_concurrency.py",
            "-q",
            "-o",
            "faulthandler_timeout=45",
            env={
                **os.environ,
                "TEST_DATABASE_URL": f"postgres://ephios:test-password@127.0.0.1:{os.environ['EPHIOS_DATABASE_PORT']}/ephios_test",
            },
            timeout=180,
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
            f"assert version('ephios-shift-coordination-plugin') == '{declared_version()}'; "
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
    run(
        "uv",
        "run",
        "--locked",
        "djlint",
        "src/ephios_shift_coordination/templates",
        "--check",
        "--lint",
    )
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
    parser.add_argument(
        "command", choices=("setup", "build", "up", "down", "e2e", "check", "import-demo")
    )
    parser.add_argument(
        "--no-admin",
        action="store_true",
        help="Import demo data without interactive administrator creation.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete this stack's data before importing, so the demo starts from scratch.",
    )
    arguments = parser.parse_args()
    command = arguments.command
    actions = {
        "setup": setup,
        "build": build,
        "up": up,
        "down": lambda: compose("down"),
        "e2e": e2e,
        "check": check,
        "import-demo": lambda: import_demo(no_admin=arguments.no_admin, reset=arguments.reset),
    }
    actions[command]()
