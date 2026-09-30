"""脚本输入校验 / 友好报错 / ivb validate(P0-3)。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from interview_video.cli import app
from interview_video.models import (
    Script,
    ScriptFileError,
    format_validation_error,
    load_script_file,
    validate_script,
)

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = [ROOT / "examples" / "sample_script.json", ROOT / "src" / "interview_video" / "data" / "sample_script.json"]

runner = CliRunner()


def _scene(**kw) -> dict:
    return {"kind": "point", "header": "开场", "narration": "口播内容。", **kw}


def _write(tmp_path: Path, data) -> Path:
    p = tmp_path / "script.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.mark.parametrize("path", SAMPLES, ids=["examples", "data"])
def test_sample_scripts_validate(path: Path) -> None:
    assert load_script_file(path).scenes


def test_empty_scenes_rejected() -> None:
    with pytest.raises(ValidationError) as ei:
        validate_script({"title": "x", "scenes": []})
    assert format_validation_error(ei.value).startswith("scenes:")


def test_blank_narration_rejected() -> None:
    with pytest.raises(ValidationError) as ei:
        validate_script({"title": "x", "scenes": [_scene(narration="   ")]})
    assert "scenes[0].narration" in format_validation_error(ei.value)


def test_qa_missing_answer_rejected() -> None:
    with pytest.raises(ValidationError) as ei:
        validate_script({"title": "x", "scenes": [_scene(kind="qa", question="问题?")]})
    msg = format_validation_error(ei.value)
    assert "scenes[0]" in msg and "answer" in msg


def test_direct_construction_stays_lenient() -> None:
    """代码内构造 / 旧 project.json 不受严格校验影响。"""
    assert Script(title="x", scenes=[]).scenes == []


def test_load_script_file_errors_have_abs_path(tmp_path: Path) -> None:
    with pytest.raises(ScriptFileError, match="不存在"):
        load_script_file(tmp_path / "nope.json")
    p = _write(tmp_path, '{"title": "x", "scenes": [')
    with pytest.raises(ScriptFileError, match="不是合法 JSON") as ei:
        load_script_file(p)
    assert str(p.resolve()) in str(ei.value)
    p = _write(tmp_path, {"title": "x", "scenes": [_scene(kind="Point")]})
    with pytest.raises(ScriptFileError) as ei:
        load_script_file(p)
    assert "script.json 格式错误" in str(ei.value)
    assert "scenes[0].kind: 须为 point/qa/ending" in str(ei.value)


def test_generate_empty_scenes_exit2_no_dir(tmp_path: Path) -> None:
    p = _write(tmp_path, {"title": "x", "scenes": []})
    out = tmp_path / "out"
    result = runner.invoke(app, ["generate", "--script-file", str(p), "--out", str(out)])
    assert result.exit_code == 2, result.output
    assert not out.exists()
    assert "scenes" in result.output


def test_generate_bad_kind_reports_path_once(tmp_path: Path) -> None:
    p = _write(tmp_path, {"title": "x", "scenes": [_scene(kind="Point")]})
    out = tmp_path / "out"
    result = runner.invoke(app, ["generate", "--script-file", str(p), "--out", str(out)])
    assert result.exit_code == 2
    flat = result.output.replace("\n", "")
    assert "scenes[0].kind" in flat
    assert flat.count("scenes[0].kind") == 1
    assert not out.exists()


def test_generate_resume_bad_script_exit2(tmp_path: Path) -> None:
    d = tmp_path / "proj"
    d.mkdir()
    (d / "script.json").write_text('{"title": "x", "scenes": []}', encoding="utf-8")
    result = runner.invoke(app, ["generate", "--resume", str(d)])
    assert result.exit_code == 2
    assert "脚本校验失败" in result.output


def test_validate_command_ok() -> None:
    result = runner.invoke(app, ["validate", str(SAMPLES[0])])
    assert result.exit_code == 0, result.output
    assert "校验通过" in result.output
    assert "预估" in result.output


def test_validate_command_bad(tmp_path: Path) -> None:
    p = _write(tmp_path, {"title": "x", "scenes": [_scene(kind="qa", question="q")]})
    result = runner.invoke(app, ["validate", str(p)])
    assert result.exit_code == 2
    assert "answer" in result.output
