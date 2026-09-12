FROM ghcr.io/ephios-dev/ephios:v0.27.0@sha256:0cebfa86ef6b95d2e2acda5837f13d353ebd795c01f8039d3253fb9b6a5aa28f

COPY *.whl /opt/shift-coordination/
RUN python - <<'PY'
from pathlib import Path

path = Path("pyproject.toml")
text = path.read_text()
for old, new in (
    ('dynamic = ["version"]', 'version = "0.27.0"'),
    ('requires-python = ">=3.11,<4"', 'requires-python = ">=3.14,<3.15"'),
):
    assert old in text
    text = text.replace(old, new, 1)
path.write_text(text)
PY
RUN uv add /opt/shift-coordination/*.whl \
    && uv run python -c 'from importlib.metadata import version; assert version("ephios") == "0.27.0"; assert version("ephios-shift-coordination-plugin") == "0.1.0"'
