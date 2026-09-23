"""evals 评测模块测试: 确定性契约检查(不联网/不调真实 LLM)。"""
from __future__ import annotations

from interview_video.evals import GOLDEN_CASES, check_script_contract
from interview_video.models import Scene, Script


def _script(scenes: list[Scene]) -> Script:
    return Script(title="测试脚本", scenes=scenes)


def _ok_scene(kind: str = "point") -> Scene:
    return Scene(kind=kind, header="标题", bullets=["要点"],
                 narration="这是一段长度合适的口播内容。" * 5)  # ~110 字内


def test_contract_passes_for_valid_script():
    scenes = [_ok_scene() for _ in range(6)]
    scenes[-1] = _ok_scene("ending")
    assert check_script_contract(_script(scenes)) == []


def test_contract_flags_scene_count():
    scenes = [_ok_scene() for _ in range(3)]
    scenes[-1] = _ok_scene("ending")
    issues = check_script_contract(_script(scenes))
    assert any("分镜数" in i for i in issues)


def test_contract_flags_overlong_header_and_bullet():
    s = _ok_scene()
    s.header = "超过十二个字的标题一定会被检查出来"
    s.bullets = ["超过十四个字的要点也一定会被检查出来的吧"]
    issues = check_script_contract(_script([s, _ok_scene("ending")]))
    assert any("header" in i for i in issues)
    assert any("bullet" in i for i in issues)


def test_contract_flags_missing_ending():
    issues = check_script_contract(_script([_ok_scene() for _ in range(6)]))
    assert any("ending" in i for i in issues)


def test_contract_flags_redline_violation():
    s = _ok_scene()
    s.narration = "这条口播说月薪5万不是问题。" + "填充内容。" * 10
    issues = check_script_contract(_script([s, _ok_scene("ending")]))
    assert any("红线" in i for i in issues)


def test_golden_cases_shape():
    assert len(GOLDEN_CASES) >= 5
    assert all({"industry", "topic_title"} <= set(c) for c in GOLDEN_CASES)
