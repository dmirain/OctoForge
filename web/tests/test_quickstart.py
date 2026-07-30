"""Tests for tools/quickstart.py — the one-command first run.

The script is stdlib-only and lives outside both packages (it has to run before
anything is installed), so it is imported by path here.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.fernet import Fernet

from octoforge_web.auth import verify_password

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "quickstart.py"
LLM_KEY = "sk-real-key"


def load_script() -> ModuleType:
    """Import quickstart.py as a module without putting tools/ on sys.path."""
    spec = importlib.util.spec_from_file_location("of_quickstart", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


quickstart = load_script()


def values_of(path: Path) -> dict[str, str]:
    """Parse the written env file."""
    return quickstart.read_values(path.read_text(encoding="utf-8"))


def test_generated_hash_verifies_against_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    # the script duplicates auth.hash_password on purpose (no imports available
    # before install); this is the test that keeps the two formats identical
    encoded = quickstart.hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_generated_key_is_a_usable_fernet_key() -> None:
    Fernet(quickstart.fernet_key().encode())


def test_written_env_is_ready_to_start(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / ".env"

    exit_code = quickstart.main(["--env", str(env), "--llm-key", LLM_KEY])

    assert exit_code == 0
    values = values_of(env)
    assert values["OF_LLM_API_KEY"] == LLM_KEY
    assert quickstart.missing_requirements(values) == []
    # the password is printed once and only its hash is stored
    printed = capsys.readouterr().out
    password = printed.split("password:")[1].strip().splitlines()[0]
    assert verify_password(password, values["OF_ADMIN_PASSWORD_HASH"])
    assert password not in env.read_text(encoding="utf-8")
    Fernet(values["OF_SECRETS_KEY"].encode())


def test_two_runs_generate_different_secrets(tmp_path: Path) -> None:
    first, second = tmp_path / "a.env", tmp_path / "b.env"

    quickstart.main(["--env", str(first), "--llm-key", LLM_KEY])
    quickstart.main(["--env", str(second), "--llm-key", LLM_KEY])

    assert values_of(first)["OF_SECRETS_KEY"] != values_of(second)["OF_SECRETS_KEY"]
    assert values_of(first)["OF_ADMIN_PASSWORD_HASH"] != values_of(second)["OF_ADMIN_PASSWORD_HASH"]


def test_existing_env_is_never_overwritten(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OF_LLM_API_KEY=mine\nOF_ADMIN_PASSWORD_HASH=hash\n", encoding="utf-8")

    exit_code = quickstart.main(["--env", str(env)])

    assert exit_code == 0
    assert env.read_text(encoding="utf-8") == "OF_LLM_API_KEY=mine\nOF_ADMIN_PASSWORD_HASH=hash\n"


def test_existing_env_with_gaps_stops_the_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    env.write_text("OF_ADMIN_PASSWORD_HASH=hash\n", encoding="utf-8")

    exit_code = quickstart.main(["--env", str(env)])

    assert exit_code == 1
    assert "OF_LLM_API_KEY" in capsys.readouterr().err


def test_placeholder_key_stops_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OF_LLM_API_KEY", raising=False)
    monkeypatch.setattr(quickstart.sys.stdin, "isatty", lambda: False)
    env = tmp_path / ".env"

    exit_code = quickstart.main(["--env", str(env)])

    assert exit_code == 1
    assert values_of(env)["OF_LLM_API_KEY"] == quickstart.PLACEHOLDER_KEY
    assert quickstart.PLACEHOLDER_KEY in capsys.readouterr().err


def test_key_comes_from_the_environment_when_no_flag_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OF_LLM_API_KEY", LLM_KEY)
    monkeypatch.setattr(quickstart.sys.stdin, "isatty", lambda: False)
    env = tmp_path / ".env"

    assert quickstart.main(["--env", str(env)]) == 0
    assert values_of(env)["OF_LLM_API_KEY"] == LLM_KEY
