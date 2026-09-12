import subprocess
import tarfile
import zipfile
from pathlib import Path


def test_build_produces_an_installable_wheel_and_minimal_container_context():
    subprocess.run(["scripts/run", "python3", "scripts/project.py", "build"], check=True)
    context = Path(".local/build")
    files = {path.name for path in context.iterdir()}
    wheel = next(context.glob("*.whl"))
    assert files == {wheel.name, "Dockerfile"}
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    assert "ephios_shift_coordination/apps.py" in names
    assert any(name.endswith(".dist-info/entry_points.txt") for name in names)
    assert all(
        name.startswith(("ephios_shift_coordination/", "ephios_shift_coordination_plugin-"))
        for name in names
    )
    with tarfile.open(next(Path("dist").glob("*.tar.gz"))) as archive:
        names = [Path(name).parts[1:] for name in archive.getnames()]
    assert all(
        parts[0]
        in {"src", "pyproject.toml", "uv.lock", "README.md", "LICENSE", "PKG-INFO", ".gitignore"}
        for parts in names
        if parts
    )
