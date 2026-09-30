"""红线扫描器测试: chains.redlines 复用 boundary 的唯一规则源。"""
from __future__ import annotations

from interview_video import boundary
from interview_video.chains.redlines import RED_LINES, scan_redlines


def test_redlines_text_contract():
    """prompt 红线文本的承重条款(与 SPECS §2 一致)。"""
    for kw in ("薪资", "歧视", "包过", "诋毁"):
        assert kw in RED_LINES


def test_scan_catches_salary_numbers():
    assert "编造薪资数字" in scan_redlines("月薪2万不是梦")
    assert "编造薪资数字" in scan_redlines("年薪 30W 起步")
    assert "编造薪资数字" in scan_redlines("起薪25k")


def test_scan_allows_vague_salary():
    assert scan_redlines("薪资因城市和公司差异较大") == []


def test_scan_catches_promise():
    assert "包过承诺" in scan_redlines("跟着做必上岸")
    assert "包过承诺" in scan_redlines("三天包过")


def test_scan_allows_normal_promise():
    assert scan_redlines("帮你提高通过率") == []


def test_scan_catches_discrimination():
    assert "歧视性表述" in scan_redlines("很多岗位只要985")
    assert "歧视性表述" in scan_redlines("35岁不要的岗位")


def test_scan_allows_neutral_discussion():
    assert scan_redlines("35岁转型并不晚, 关键是方法") == []
    assert scan_redlines("学历只是敲门砖之一") == []


def test_single_source_of_truth():
    assert scan_redlines is boundary.scan_redlines


def test_scan_allows_encouraging_or_neutral_phrases():
    assert scan_redlines("35岁不要焦虑，经验就是优势") == []
    assert scan_redlines("很多人想一次过，其实没必要") == []
    assert scan_redlines("不要只盯着985的光环") == []
    assert scan_redlines("没人能保证一次过，准备才是关键") == []
    assert scan_redlines("评估维度包括通过率和稳定性") == []
    assert scan_redlines("确保过程顺畅再提交") == []


def test_scan_catches_redline_regressions():
    assert "编造薪资数字" in scan_redlines("期望薪资可以说15k到20k之间吗")
    assert "编造薪资数字" in scan_redlines("15K-20K的岗位")
    assert "包过承诺" in scan_redlines("我们保证一次过")
    assert "包过承诺" in scan_redlines("报名就包你上岸")
    assert "歧视性表述" in scan_redlines("公司35岁以上不要")
    assert "歧视性表述" in scan_redlines("这家公司明说了，35岁一律淘汰")
    assert "歧视性表述" in scan_redlines("只招985")


def test_exclusion_only_masks_overlapping_span():
    assert "歧视性表述" in scan_redlines("35岁不要焦虑，但有的公司35岁就不要了")


def test_negated_promise_passes_but_affirmative_blocks():
    assert scan_redlines("谁能保证一次过呢") == []
    assert "包过承诺" in scan_redlines("我们能保证一次过")


# ---- G06: 扩展红线语料(必须拦截 / 必须放行) ----
MUST_CATCH = [
    ("应届生月薪过万不是梦", "编造薪资数字"),
    ("起薪一般在8k左右", "编造薪资数字"),
    ("年薪轻松百万", "编造薪资数字"),
    ("保证你能过面试", "包过承诺"),
    ("跟着练肯定能拿offer", "包过承诺"),
    ("包你上岸", "包过承诺"),
    ("女生不适合做程序员", "歧视性表述"),
    ("35岁以上基本没人要", "歧视性表述"),
    ("大专生不要投大厂", "歧视性表述"),
    ("加我微信领取资料", "私域引流"),
    ("V我领取面试题库", "私域引流"),
    ("私信领取简历模板", "私域引流"),
    ("评论区扣1进群", "私域引流"),
    ("vx号在主页", "私域引流"),
]

MUST_PASS = [
    "35岁不要焦虑",
    "35岁不要焦虑，经验就是优势",
    "女生不要自卑，技术面前人人平等",
    "谁说女生不适合做程序员",
    "别信35岁没人要这种说法",
    "薪资因城市和公司差异较大",
    "月薪多少因城市而异，建议查公开数据",
    "没人能保证你一定能过面试",
    "关注我，评论区交流你的面试经历",
    "融入群体很重要",
    "可以用微信支付报名费吗",
]


def test_expanded_redline_corpus_catches():
    for text, rule in MUST_CATCH:
        assert rule in scan_redlines(text), text


def test_expanded_redline_corpus_passes():
    for text in MUST_PASS:
        assert scan_redlines(text) == [], (text, scan_redlines(text))
