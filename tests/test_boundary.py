"""模型输出边界体系 (boundary.py) 单元测试:
覆盖 Schema 边界、长度与结构边界、业务红线边界及自修复回路全链路。
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from interview_video.boundary import (
    BoundaryChecker,
    BoundaryRules,
    BoundaryViolationError,
    clamp_duration,
    enforce_boundary,
    enforce_boundary_sync,
    extract_json_text,
    fix_script_deterministic,
    parse_json_lenient,
    scan_object_redlines,
    scan_redlines,
    truncate_narration,
)
from interview_video.models import (
    EpisodeOutline,
    Scene,
    Script,
    Topic,
)

# ===========================================================================
# 1. Schema 边界测试: 容错 JSON 提取与 Pydantic 校验
# ===========================================================================

def test_extract_json_plain():
    text = '{"name": "test", "count": 10}'
    assert extract_json_text(text) == text


def test_extract_json_with_markdown_fence():
    text = """这是模型的回答:
```json
{
  "title": "测试标题",
  "scenes": []
}
```
希望对你有帮助！"""
    extracted = extract_json_text(text)
    assert '"title": "测试标题"' in extracted


def test_extract_json_with_think_tags():
    text = """<think>
思考中...我们需要生成一个包含 title 的 JSON。
不要输出其他内容。
</think>
```json
{"title": "思考后的标题"}
```"""
    extracted = extract_json_text(text)
    assert extracted == '{"title": "思考后的标题"}'


def test_extract_json_bracket_balance_with_nested_and_escapes():
    text = '前言文字 {"text": "带有 \\"转义引号\\" 和 {嵌套} 字符", "list": [1, 2, {"a": 3}]} 结尾文字'
    extracted = extract_json_text(text)
    assert extracted.startswith("{") and extracted.endswith("}")
    data = parse_json_lenient(text)
    assert data["list"][2]["a"] == 3


def test_extract_json_no_json_raises():
    with pytest.raises(ValueError, match="未找到 JSON"):
        extract_json_text("这里没有任何JSON数据")


def test_extract_json_unclosed_bracket_raises():
    with pytest.raises(ValueError, match="未闭合"):
        extract_json_text('{"title": "没有闭合')


def test_parse_json_lenient_trailing_comma():
    # 悬挂逗号
    text = '{"title": "测试", "items": [1, 2, ], }'
    data = parse_json_lenient(text, allow_trailing_commas=True)
    assert data["title"] == "测试"
    assert data["items"] == [1, 2]


class SimpleSchema(BaseModel):
    title: str
    count: int = Field(ge=1)


def test_boundary_checker_schema_failure():
    checker = BoundaryChecker()
    # 字段缺失与类型错误
    raw = '{"count": -5}'
    inst, violations = checker.check(raw, SimpleSchema)
    assert inst is None
    assert len(violations) >= 1
    categories = [v.category for v in violations]
    assert "schema" in categories


# ===========================================================================
# 2. 长度与结构边界测试: 分镜数、口播字数、标题、要点、结尾、章节时长
# ===========================================================================

def _make_valid_scene(i: int, is_ending: bool = False) -> dict:
    kind = "ending" if is_ending else "point"
    header = f"标题{i}"
    bullets = [f"要点{i}_A", f"要点{i}_B"]
    # 80 字合规口播 (65~110 字区间)
    narration = f"这是第{i}个分镜的完整口播文本。在求职面试过程中，我们要充分展示自己的专业技能和综合素养，回答问题条理清晰，结构严密，让面试官看到你的实际解决问题能力。"
    assert 65 <= len(narration) <= 110
    return {
        "kind": kind,
        "header": header,
        "bullets": bullets,
        "narration": narration,
    }


def test_script_structure_compliant():
    checker = BoundaryChecker()
    scenes = [_make_valid_scene(i, is_ending=(i == 5)) for i in range(1, 6)]
    data = {
        "title": "程序员面试全攻略",
        "summary": "结构化表达技巧",
        "scenes": scenes,
    }
    raw = Script.model_validate(data).model_dump_json()
    script, violations = checker.check(raw, Script)
    assert len(violations) == 0
    assert script is not None
    assert len(script.scenes) == 5
    assert script.scenes[-1].kind == "ending"


def test_script_scene_count_violations():
    checker = BoundaryChecker()
    # 只有 3 镜 (< 5 镜)
    scenes_too_few = [_make_valid_scene(i, is_ending=(i == 3)) for i in range(1, 4)]
    data_few = {"title": "标题", "summary": "", "scenes": scenes_too_few}
    inst, violations = checker.check(Script.model_validate(data_few).model_dump_json(), Script)
    assert any(v.field == "scenes" and "不足" in v.message for v in violations)

    # 9 镜 (> 8 镜)
    scenes_too_many = [_make_valid_scene(i, is_ending=(i == 9)) for i in range(1, 10)]
    data_many = {"title": "标题", "summary": "", "scenes": scenes_too_many}
    inst, violations = checker.check(Script.model_validate(data_many).model_dump_json(), Script)
    assert any(v.field == "scenes" and "过多" in v.message for v in violations)


def test_scene_header_length_violation():
    checker = BoundaryChecker()
    scenes = [_make_valid_scene(i, is_ending=(i == 5)) for i in range(1, 6)]
    # 标题超过 12 字 (14字)
    scenes[0]["header"] = "这是一个超级超级超级长的画面标题"
    data = {"title": "标题", "summary": "", "scenes": scenes}
    inst, violations = checker.check(Script.model_validate(data).model_dump_json(), Script)
    assert any(v.field == "scenes[0].header" and "画面标题超长" in v.message for v in violations)


def test_scene_bullets_length_violation():
    checker = BoundaryChecker()
    scenes = [_make_valid_scene(i, is_ending=(i == 5)) for i in range(1, 6)]
    # bullet 超过 14 字
    scenes[1]["bullets"] = ["这是一个超过十四个字符的超长屏显要点"]
    data = {"title": "标题", "summary": "", "scenes": scenes}
    inst, violations = checker.check(Script.model_validate(data).model_dump_json(), Script)
    assert any("bullets" in v.field and "超长" in v.message for v in violations)


def test_scene_narration_length_violation():
    checker = BoundaryChecker()
    scenes = [_make_valid_scene(i, is_ending=(i == 5)) for i in range(1, 6)]
    # 口播过短 (< 65 字)
    scenes[0]["narration"] = "口播太短了只有十个字。"
    # 口播过长 (> 110 字, 120字)
    scenes[1]["narration"] = (
        "这是一段极其漫长而且超出了字数上限的口播内容。在面试中如果不控制语速和口播时长，"
        "观众会觉得拖沓冗长失去耐心。短视频的核心是节奏感，每一镜的口播字数必须严格卡在"
        "六十五到一百一十个字之间，才能保证信息密度与观感体验的完美平衡。"
    )
    data = {"title": "标题", "summary": "", "scenes": scenes}
    inst, violations = checker.check(Script.model_validate(data).model_dump_json(), Script)
    fields = [v.field for v in violations]
    assert "scenes[0].narration" in fields
    assert "scenes[1].narration" in fields


def test_scene_ending_kind_violation():
    checker = BoundaryChecker()
    # 最后一镜不是 ending 而是 point
    scenes = [_make_valid_scene(i, is_ending=False) for i in range(1, 6)]
    data = {"title": "标题", "summary": "", "scenes": scenes}
    inst, violations = checker.check(Script.model_validate(data).model_dump_json(), Script)
    assert any(v.field.endswith(".kind") and "ending" in v.message for v in violations)


def test_chapter_duration_clamped():
    checker = BoundaryChecker(BoundaryRules(min_chapter_sec=60, max_chapter_sec=600, clamp_chapter_sec=True))
    outline_data = {
        "episode_index": 1,
        "title": "第一集",
        "chapters": [
            {"title": "第1章", "est_sec": 30},   # < 60s, 应被 clamp 到 60
            {"title": "第2章", "est_sec": 900},  # > 600s, 应被 clamp 到 600
            {"title": "第3章", "est_sec": 180},  # 合规
        ],
    }
    raw = EpisodeOutline.model_validate(outline_data).model_dump_json()
    outline, violations = checker.check(raw, EpisodeOutline)
    assert outline is not None
    # clamp 模式下自动修正，不产生硬性违规
    assert len(violations) == 0
    assert outline.chapters[0].est_sec == 60
    assert outline.chapters[1].est_sec == 600
    assert outline.chapters[2].est_sec == 180


def test_chapter_duration_strict_unclamped_raises_violations():
    checker = BoundaryChecker(BoundaryRules(min_chapter_sec=60, max_chapter_sec=600, clamp_chapter_sec=False))
    outline_data = {
        "episode_index": 1,
        "title": "第一集",
        "chapters": [
            {"title": "第1章", "est_sec": 20},
        ],
    }
    raw = EpisodeOutline.model_validate(outline_data).model_dump_json()
    outline, violations = checker.check(raw, EpisodeOutline)
    assert len(violations) == 1
    assert violations[0].category == "length"
    assert "20s" in violations[0].message


# ===========================================================================
# 3. 业务红线边界测试: 薪资、包过、歧视、诋毁
# ===========================================================================

def test_redlines_catches_all_four_categories():
    # 1. 薪资数字
    assert "编造薪资数字" in scan_redlines("月薪2万起步")
    assert "编造薪资数字" in scan_redlines("年薪 35W 不是梦")
    assert "编造薪资数字" in scan_redlines("起薪25k")

    # 2. 包过承诺
    assert "包过承诺" in scan_redlines("跟着这套课三天包过")
    assert "包过承诺" in scan_redlines("保offer稳拿录用")
    assert "包过承诺" in scan_redlines("必定上岸")

    # 3. 歧视表述
    assert "歧视性表述" in scan_redlines("很多岗位只要985")
    assert "歧视性表述" in scan_redlines("35岁不要的岗位别投")
    assert "歧视性表述" in scan_redlines("限男性应聘")

    # 4. 诋毁贬损
    assert "诋毁贬损" in scan_redlines("某大厂太垃圾了千万别去")
    assert "诋毁贬损" in scan_redlines("这家黑心企业到处坑人")
    assert "诋毁贬损" in scan_redlines("骗子公司毫无诚信")


def test_redlines_allows_safe_expressions():
    # 正常、中立的表述不应被误伤
    assert scan_redlines("薪资因城市和公司差异较大") == []
    assert scan_redlines("待遇面议，视个人能力定级") == []
    assert scan_redlines("这门技巧可以有效帮助提高通过率") == []
    assert scan_redlines("35岁转型并不晚，关键在核心竞争力") == []
    assert scan_redlines("学历只是敲门砖之一，能力更关键") == []
    assert scan_redlines("理性分析企业的用人制度和岗位适配度") == []


def test_redlines_scanning_on_model():
    topic = Topic(
        title="月薪3万面试攻略",  # 命中薪资
        industry="互联网",
        audience="所有人",
        hook="跟着我保证通过",    # 命中包过
        key_points=["只要985本科生"], # 命中歧视
    )
    violations = scan_object_redlines(topic)
    assert len(violations) == 3
    fields = [v.field for v in violations]
    assert "title" in fields
    assert "hook" in fields
    assert "key_points[0]" in fields
    categories = [v.category for v in violations]
    assert all(c == "redline" for c in categories)


# ===========================================================================
# 4. 自修复回路 (Self-Repair Loop) 与 enforce_boundary 测试
# ===========================================================================

@pytest.mark.asyncio
async def test_enforce_boundary_direct_pass():
    call_count = 0
    valid_data = {
        "title": "前端开发面试准备",
        "industry": "互联网",
        "audience": "前端求职者",
        "hook": "如何搞定Vue3响应式源码题？",
        "key_points": ["响应式原理", "Proxy对比", "手写实现"],
    }

    async def fake_caller(messages, temperature=0.7):
        nonlocal call_count
        call_count += 1
        return Topic.model_validate(valid_data).model_dump_json()

    topic = await enforce_boundary(
        "请生成一个选题",
        fake_caller,
        Topic,
        temperature=0.7,
    )
    assert topic.title == "前端开发面试准备"
    assert call_count == 1  # 一次通过，零额外修复调用


@pytest.mark.asyncio
async def test_enforce_boundary_self_repairs_redline_and_length():
    call_count = 0
    temperatures_received = []

    bad_data = {
        "title": "月薪5万前端面试攻略",  # 违规: 薪资红线
        "industry": "互联网",
        "audience": "求职者",
        "hook": "包过面试技巧",          # 违规: 包过承诺
        "key_points": ["第一点"],
    }
    good_data = {
        "title": "高薪架构师面试深度剖析", # 修复后合规
        "industry": "互联网",
        "audience": "求职者",
        "hook": "如何展现架构设计深度？",
        "key_points": ["系统拆分", "高可用容灾", "链路治理"],
    }

    async def fake_caller(messages, temperature=0.7):
        nonlocal call_count
        call_count += 1
        temperatures_received.append(temperature)
        if call_count == 1:
            # 首次返回违规数据
            return Topic.model_validate(bad_data).model_dump_json()
        # 修复调用返回纠正后数据
        # 验证修复 prompt 中包含了违规反馈与上一轮输出
        last_user_msg = messages[-1]["content"]
        assert "【模型输出边界违规诊断与自修复指令】" in last_user_msg
        assert "编造薪资数字" in last_user_msg
        assert "包过承诺" in last_user_msg
        return Topic.model_validate(good_data).model_dump_json()

    topic = await enforce_boundary(
        "生成选题",
        fake_caller,
        Topic,
        max_repairs=1,
        temperature=0.8,
        repair_temperature=0.1,
    )
    assert call_count == 2
    assert temperatures_received == [0.8, 0.1]  # 温度成功调低
    assert topic.title == "高薪架构师面试深度剖析"


@pytest.mark.asyncio
async def test_enforce_boundary_exhausted_raises_first_scene_error():
    call_count = 0
    bad_data = {
        "title": "三天包过保offer秘籍",  # 持续命中包过
        "industry": "互联网",
        "audience": "求职者",
        "hook": "保证一次过",
        "key_points": [],
    }

    async def fake_caller(messages, temperature=0.7):
        nonlocal call_count
        call_count += 1
        return Topic.model_validate(bad_data).model_dump_json()

    with pytest.raises(BoundaryViolationError) as exc_info:
        await enforce_boundary(
            "生成选题",
            fake_caller,
            Topic,
            max_repairs=1,
        )

    err = exc_info.value
    assert call_count == 2
    assert "三天包过" in err.raw_output  # 完整保留第一现场原始输出
    assert err.repaired_output is not None
    assert len(err.violations) >= 1
    summary = err.summary()
    assert summary["violations_count"] >= 1
    assert "redline" in summary["categories"]


def test_enforce_boundary_sync_wrapper():
    # 同步版本兼容
    valid_data = {
        "title": "测试同步",
        "industry": "教育",
        "audience": "教师",
        "hook": "钩子",
        "key_points": ["要点1"],
    }

    def sync_caller(messages):
        return Topic.model_validate(valid_data).model_dump_json()

    res = enforce_boundary_sync(
        "生成选题",
        sync_caller,
        Topic,
    )
    assert res.title == "测试同步"


# ===========================================================================
# 5. 辅助算子测试 (clamp / truncate / fix_script)
# ===========================================================================

def test_clamp_duration():
    assert clamp_duration(30, 60, 600) == 60
    assert clamp_duration(800, 60, 600) == 600
    assert clamp_duration(150, 60, 600) == 150


def test_truncate_narration():
    # 正常长度不截断
    short_text = "这是一段很短的口播。"
    assert truncate_narration(short_text, max_chars=110) == short_text

    # 超长文本截断到句号
    long_text = "第一句话非常关键。" + "第二句话也非常重要。" * 6
    truncated = truncate_narration(long_text, max_chars=80, min_chars=30)
    assert len(truncated) <= 80
    assert truncated.endswith("。")


def test_fix_script_deterministic():
    scenes = [
        Scene(
            kind="point",
            header="标题",
            bullets=["要点"],
            narration="超长口播" + "这是一段持续延长的长句子，用于测试截断功能。" * 5,
        ),
        Scene(
            kind="point", # 末镜不是 ending
            header="结尾",
            bullets=[],
            narration="这是最后一镜的口播文本，请大家记得点赞关注支持我们一下。",
        ),
    ]
    script = Script(title="测试", scenes=scenes, summary="")
    fixed = fix_script_deterministic(script)
    assert len(fixed.scenes[0].narration) <= 110
    assert fixed.scenes[-1].kind == "ending"


# ---- G07/G10: 人工脚本/元数据体检 + qa 问答长度 ----

def test_qa_question_answer_length_checked():
    from interview_video.boundary import BoundaryChecker
    from interview_video.models import Scene, Script

    script = Script(title="t", scenes=[
        Scene(kind="qa", header="h", narration="n" * 80, question="问" * 41, answer="答" * 61),
    ])
    fields = {v.field for v in BoundaryChecker().check_length_and_structure(script)}
    assert "scenes[0].question" in fields and "scenes[0].answer" in fields

    ok = Script(title="t", scenes=[
        Scene(kind="qa", header="h", narration="n" * 80, question="问" * 40, answer="答" * 60),
    ])
    fields = {v.field for v in BoundaryChecker().check_length_and_structure(ok)}
    assert not {"scenes[0].question", "scenes[0].answer"} & fields


def test_lint_script_reports_redlines_and_length():
    from interview_video.boundary import lint_script
    from interview_video.models import Scene, Script

    script = Script(title="t", scenes=[
        Scene(kind="point", header="超长的画面标题超过十二个字了", narration="加我微信领取资料。" * 8),
    ])
    warnings = lint_script(script)
    joined = "\n".join(warnings)
    assert "私域引流" in joined and "scenes[0].header" in joined and "分镜数量不足" in joined


def test_lint_meta_reports_redlines_and_length():
    from interview_video.boundary import lint_meta
    from interview_video.models import BiliMeta

    warnings = lint_meta(BiliMeta(title="包你上岸" + "字" * 80, desc="x", tags=["t"] * 11))
    joined = "\n".join(warnings)
    assert "包过承诺" in joined and "标题超长" in joined and "标签过多" in joined
    assert lint_meta(BiliMeta(title="面试自我介绍怎么说", desc="好内容", tags=["面试"])) == []
