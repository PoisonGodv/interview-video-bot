"""开头钩子确定性规则 + 示例脚本同步 + qa 模板缺字段不显示 None。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from interview_video.boundary import BoundaryChecker
from interview_video.models import Scene, Script
from interview_video.render import _build_env, _render_html

ROOT = Path(__file__).resolve().parent.parent
_BODY = "在求职面试过程中，我们要充分展示自己的专业技能和综合素养，回答问题条理清晰，结构严密，让面试官看到你的实际解决问题能力。"


def _scenes(first_narration: str) -> list[dict]:
    scenes = [{"kind": "point", "header": f"标题{i}", "narration": f"第{i}镜。" + _BODY} for i in range(5)]
    scenes[0]["narration"] = first_narration
    scenes[-1]["kind"] = "ending"
    return scenes


def _hook_violations(first_narration: str):
    vs = BoundaryChecker().check_length_and_structure({"scenes": _scenes(first_narration)})
    return [v for v in vs if v.field == "scenes[0].narration" and v.category == "structure"]


@pytest.mark.parametrize("opening", [
    "大家好，",
    "哈喽各位，",
    "嗨，朋友们，",
    "我是面试充电站的小王，",
    "这里是面试充电站，",
    "  “大家好”，",
])
def test_greeting_opening_rejected(opening):
    vs = _hook_violations(opening + _BODY)
    assert len(vs) == 1
    assert "问候" in vs[0].message and vs[0].fix_hint


@pytest.mark.parametrize("opening", [
    "自我介绍背简历，等于直接放弃。",
    "90%的人都答错了这道题。",
    "面试官最怕听到的一句话。",
    "今天我是来泼冷水的，",  # 非句首的「我是」不算
])
def test_hook_opening_accepted(opening):
    assert _hook_violations(opening + _BODY) == []


def test_greeting_only_checked_on_first_scene():
    scenes = _scenes("三个坑，九成程序员都踩过。" + _BODY)
    scenes[2]["narration"] = "大家好，" + _BODY
    vs = BoundaryChecker().check_length_and_structure({"scenes": scenes})
    assert not [v for v in vs if "问候" in v.message]


def test_greeting_triggers_violation_in_full_check():
    raw = json.dumps({"title": "t", "scenes": _scenes("大家好，" + _BODY)}, ensure_ascii=False)
    inst, vs = BoundaryChecker().check(raw, Script)
    assert inst is not None
    assert any(v.field == "scenes[0].narration" for v in vs)


@pytest.mark.parametrize("path", [
    ROOT / "examples" / "sample_script.json",
    ROOT / "src" / "interview_video" / "data" / "sample_script.json",
])
def test_sample_scripts_have_hook_opening(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert _hook_violations(data["scenes"][0]["narration"]) == []
    assert "错误回答" in "".join(s["narration"] for s in data["scenes"])
    assert "评论区" in data["scenes"][-1]["narration"]


def _render_qa(scene: Scene) -> str:
    script = Script(title="t", scenes=[scene])
    return _render_html(
        _build_env(), "qa.html", brand="b", title="t", script=script,
        scene=scene, index=0, total=1,
    )


def test_qa_html_missing_question_and_answer_no_none():
    html = _render_qa(Scene(kind="qa", header="高频题", narration="x"))
    assert "None" not in html
    assert ">Q<" not in html and ">A<" not in html


def test_qa_html_answer_falls_back_to_bullets():
    html = _render_qa(Scene(kind="qa", header="高频题", bullets=["STAR法则", "量化结果"],
                            question="最难的技术难题？", narration="x"))
    assert "None" not in html
    assert "最难的技术难题？" in html
    assert "STAR法则；量化结果" in html
