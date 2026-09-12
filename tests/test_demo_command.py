import os

import pytest

from scripts import project


def test_demo_import_offers_native_admin_creation_after_loading_data(monkeypatch):
    actions = []
    monkeypatch.setattr(project, "up", lambda: actions.append("start"))
    monkeypatch.setattr(project, "compose", lambda *args, **kwargs: actions.append(args))
    project.import_demo()
    assert actions[0] == "start"
    assert actions[1][:5] == ("exec", "-T", "app", "ephios", "shell")
    assert "seed_demo()" in actions[1][-1]
    assert actions[2] == ("exec", "app", "ephios", "createsuperuser")


def test_unattended_demo_import_does_not_prompt_for_admin(monkeypatch):
    actions = []
    monkeypatch.setattr(project, "up", lambda: None)
    monkeypatch.setattr(project, "compose", lambda *args, **kwargs: actions.append(args))
    project.import_demo(no_admin=True)
    assert len(actions) == 1


def test_automated_stack_is_separate_and_restores_environment_on_failure(monkeypatch):
    monkeypatch.setenv("EPHIOS_STACK", "development")
    monkeypatch.setenv("EPHIOS_HTTP_PORT", "8111")
    monkeypatch.delenv("EPHIOS_MAIL_PORT", raising=False)
    monkeypatch.setenv("EPHIOS_TEST_HTTP_PORT", "8222")
    with pytest.raises(RuntimeError), project.e2e_environment():
        assert os.environ["EPHIOS_STACK"] == "test"
        assert os.environ["EPHIOS_HTTP_PORT"] == "8222"
        raise RuntimeError("Interrupted test")
    assert os.environ["EPHIOS_STACK"] == "development"
    assert os.environ["EPHIOS_HTTP_PORT"] == "8111"
    assert "EPHIOS_MAIL_PORT" not in os.environ
