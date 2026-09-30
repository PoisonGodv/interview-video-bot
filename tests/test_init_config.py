"""P1-6: ivb init / 用户级配置 / OUT_ROOT 解析 / doctor 提示。"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from interview_video import doctor as doctor_mod
from interview_video.cli import app
from interview_video.config import init_user_env, load_settings, resolve_env_file, user_env_file


@pytest.fixture
def env(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    cfg = tmp_path / "cfg"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("IVB_CONFIG_DIR", str(cfg))
    for k in ("IVB_ENV_FILE", "OUT_ROOT", "BRAND", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(cwd)
    return {"home": home, "cfg": cfg, "cwd": cwd}


def test_user_config_dir_override(env) -> None:
    assert user_env_file() == env["cfg"] / ".env"


def test_init_writes_0600_with_absolute_out_root(env) -> None:
    path = init_user_env()
    assert path == env["cfg"] / ".env"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    out_lines = [ln for ln in text.splitlines() if ln.startswith("OUT_ROOT=")]
    assert out_lines == [f"OUT_ROOT={env['home'] / 'ivb-output'}"]
    s = load_settings()
    assert s._env_path == path.resolve()
    assert s.out_root == env["home"] / "ivb-output"
    assert s.llm_api_key is None


def test_init_refuses_overwrite_without_force(env) -> None:
    path = init_user_env()
    path.write_text("BRAND=改过\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        init_user_env()
    assert path.read_text(encoding="utf-8") == "BRAND=改过\n"
    path.chmod(0o644)
    init_user_env(force=True)
    assert "OUT_ROOT=" in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_cli_init(env) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    assert (env["cfg"] / ".env").is_file()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 1 and "--force" in r.output
    r = runner.invoke(app, ["init", "--force", "--out-root", str(env["home"] / "vids")])
    assert r.exit_code == 0, r.output
    assert load_settings().out_root == env["home"] / "vids"


def test_relative_out_root_in_user_env_resolves_to_home(env) -> None:
    env["cfg"].mkdir()
    (env["cfg"] / ".env").write_text("OUT_ROOT=videos\n", encoding="utf-8")
    assert load_settings().out_root == env["home"] / "videos"


def test_cwd_env_still_wins_and_keeps_relative_out_root(env) -> None:
    init_user_env()
    Path(".env").write_text("BRAND=当前目录\nOUT_ROOT=output\n", encoding="utf-8")
    assert resolve_env_file() == (env["cwd"] / ".env").resolve()
    s = load_settings()
    assert s.brand == "当前目录"
    assert s.out_root == Path("output")


def test_doctor_hints_ivb_init_when_no_env(env) -> None:
    checks = {c.name: c for c in doctor_mod.check_config(load_settings())}
    c = checks[".env"]
    assert not c.ok and not c.required
    assert "ivb init" in c.detail and "LLM_API_KEY" in c.detail
