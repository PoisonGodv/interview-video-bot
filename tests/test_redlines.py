"""红线扫描器测试: 保守正则, 宁可漏报不误伤。"""
from __future__ import annotations

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
    assert "承诺结果" in scan_redlines("跟着做必上岸")
    assert "承诺结果" in scan_redlines("三天包过")


def test_scan_allows_normal_promise():
    assert scan_redlines("帮你提高通过率") == []


def test_scan_catches_discrimination():
    assert "歧视性表述" in scan_redlines("很多岗位只要985")
    assert "歧视性表述" in scan_redlines("35岁不要的岗位")


def test_scan_allows_neutral_discussion():
    assert scan_redlines("35岁转型并不晚, 关键是方法") == []
    assert scan_redlines("学历只是敲门砖之一") == []
