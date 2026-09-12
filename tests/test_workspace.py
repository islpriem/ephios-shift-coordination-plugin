import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_tool_environment_keeps_writable_paths_in_the_workspace():
    result = subprocess.run(
        [
            "scripts/run",
            "python3",
            "-c",
            "import json, os; print(json.dumps(dict(os.environ)))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    environment = json.loads(result.stdout)
    root = Path.cwd()
    for key in (
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "PIP_CACHE_DIR",
        "XDG_CACHE_HOME",
        "TMPDIR",
        "PLAYWRIGHT_BROWSERS_PATH",
        "COVERAGE_FILE",
    ):
        assert Path(environment[key]).resolve().is_relative_to(root), key


@pytest.mark.parametrize("path", [".cache", ".cache/uv", ".local/tmp"])
def test_tool_launcher_rejects_paths_outside_its_workspace(tmp_path, path):
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2("scripts/run", root / "scripts/run")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / path
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    result = subprocess.run([str(root / "scripts/run"), "true"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Workspace path escapes the project" in result.stderr
