"""批量生产: 失败记账/续跑旧目录/致命错误中止/定时发布时段分配/选题补充/跨进程锁。"""
from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from interview_video import backlog as backlog_mod
from interview_video import cli as cli_mod
from interview_video.cli import app
from interview_video.config import Settings
from interview_video.models import Topic

runner = CliRunner()
SAMPLE_SCRIPT = Path(__file__).resolve().parents[1] / "src" / "interview_video" / "data" / "sample_script.json"


def _settings(monkeypatch, tmp_path: Path, **kw) -> Settings:
    kw.setdefault("out_root", tmp_path / "output")
    s = Settings(_env_file=None, **kw)
    monkeypatch.setattr(cli_mod, "load_settings", lambda: s)
    return s


def _ts(d: date, h: int, m: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, h, m).timestamp())


# ---------- 时段分配 ----------

def test_parse_slots() -> None:
    assert backlog_mod.parse_slots("20:00, 12:00,20:00") == [(12, 0), (20, 0)]
    for bad in ("", "25:00", "8点"):
        with pytest.raises(ValueError):
            backlog_mod.parse_slots(bad)


def test_next_slot_respects_2h_lower_bound() -> None:
    today = date(2026, 10, 1)
    now = _ts(today, 19)  # 19:00: 今天 20:00 不足 2 小时
    assert backlog_mod.next_slot([(20, 0)], set(), now=now) == _ts(today + timedelta(days=1), 20)
    now = _ts(today, 10)
    assert backlog_mod.next_slot([(12, 0), (20, 0)], set(), now=now) == _ts(today, 20)


def test_next_slot_skips_taken() -> None:
    today = date(2026, 10, 1)
    now = _ts(today, 8)
    taken = {_ts(today, 12), _ts(today, 20)}
    assert backlog_mod.next_slot([(12, 0), (20, 0)], taken, now=now) == _ts(today + timedelta(days=1), 12)


def test_next_slot_start_and_15_day_bound() -> None:
    today = date(2026, 10, 1)
    now = _ts(today, 8)
    start = today + timedelta(days=3)
    assert backlog_mod.next_slot([(20, 0)], set(), start=start, now=now) == _ts(start, 20)
    # start 超出 15 天窗口 → 无可用
    assert backlog_mod.next_slot([(20, 0)], set(), start=today + timedelta(days=16), now=now) is None
    # 窗口内全部被占 → None; 最后一个合法时刻不超过 now+15 天
    all_days = {_ts(today + timedelta(days=i), 20) for i in range(17)}
    assert backlog_mod.next_slot([(20, 0)], all_days, now=now) is None
    last = backlog_mod.next_slot([(20, 0)], all_days - {_ts(today + timedelta(days=15), 20)}, now=now)
    assert last is None  # 第 15 天 20:00 > now(08:00)+15 天
    ok = _ts(today + timedelta(days=15), 7)
    assert backlog_mod.next_slot([(7, 0)], set(), start=today + timedelta(days=15), now=now) == ok


def test_taken_dtimes(tmp_path: Path) -> None:
    for name, rec in [("a", {"bvid": "BV1", "dtime": 111}), ("b", {"bvid": "BV2"}), ("c", "坏")]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "upload.json").write_text(json.dumps(rec), encoding="utf-8")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "upload.json").write_text("{bad", encoding="utf-8")
    assert backlog_mod.taken_dtimes(tmp_path) == {111}


# ---------- 失败记账 ----------

def test_mark_failed_counts_and_pending_filter(tmp_path: Path) -> None:
    s = Settings(_env_file=None, out_root=tmp_path / "output")
    backlog_mod.add_topics(s, [Topic(title="题A", industry="互联网"), Topic(title="题B", industry="互联网")])
    for i in range(1, 4):
        assert backlog_mod.mark_failed(s, "题A", "互联网", tmp_path / "x", f"err{i}") == i
    assert backlog_mod.mark_failed(s, "题A", "互联网", None, "auth", count=False) == 3
    item = next(i for i in backlog_mod.load_backlog(s) if i.title == "题A")
    assert item.last_error == "auth" and item.project == str(tmp_path / "x") and item.status == "new"
    assert [i.title for i in backlog_mod.pending(s, max_attempts=3)] == ["题B"]
    assert [i.title for i in backlog_mod.pending(s)] == ["题A", "题B"]


def test_backlog_lock_file_used(tmp_path: Path) -> None:
    s = Settings(_env_file=None, out_root=tmp_path / "output")
    backlog_mod.add_topics(s, [Topic(title="t", industry="i")])
    assert (s.out_root / "topics_backlog.json.lock").exists()


def test_batch_lock_is_exclusive(tmp_path: Path) -> None:
    pytest.importorskip("fcntl")
    s = Settings(_env_file=None, out_root=tmp_path / "output")
    with backlog_mod.batch_lock(s):
        with pytest.raises(backlog_mod.BatchLockedError):
            with backlog_mod.batch_lock(s):
                pass
    with backlog_mod.batch_lock(s):
        pass


def test_batch_fail_then_resume_old_dir(monkeypatch, tmp_path: Path) -> None:
    s = _settings(monkeypatch, tmp_path)
    backlog_mod.add_topics(s, [Topic(title="题0", industry="互联网")])
    calls: list[dict] = []

    async def fail_after_script(settings, out_dir, industry="互联网", topic_title=None, script=None, **kw):
        calls.append({"out_dir": out_dir, "script": script})
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(SAMPLE_SCRIPT, out_dir / "script.json")
        raise RuntimeError("渲染炸了")

    monkeypatch.setattr(cli_mod, "run_pipeline", fail_after_script)
    r = runner.invoke(app, ["batch", "-n", "1"])
    assert r.exit_code == 1 and "失败#1" in r.output
    first_dir = calls[0]["out_dir"]
    assert calls[0]["script"] is None
    assert (first_dir / "error.log").exists()
    item = backlog_mod.load_backlog(s)[0]
    assert item.attempts == 1 and item.project == str(first_dir)

    async def ok(settings, out_dir, industry="互联网", topic_title=None, script=None, **kw):
        calls.append({"out_dir": out_dir, "script": script})
        backlog_mod.mark_used(settings, topic_title, industry, out_dir)
        return SimpleNamespace(duration_total=10.0)

    monkeypatch.setattr(cli_mod, "run_pipeline", ok)
    r = runner.invoke(app, ["batch", "-n", "1"])
    assert r.exit_code == 0, r.output
    assert "续跑" in r.output
    assert calls[1]["out_dir"] == first_dir and calls[1]["script"] is not None
    assert not (first_dir / "error.log").exists()
    assert backlog_mod.pending(s) == []


def test_batch_skips_after_max_attempts(monkeypatch, tmp_path: Path) -> None:
    s = _settings(monkeypatch, tmp_path)
    backlog_mod.add_topics(s, [Topic(title="坏题", industry="互联网"), Topic(title="好题", industry="互联网")])
    for _ in range(3):
        backlog_mod.mark_failed(s, "坏题", "互联网", None, "x")
    ran: list[str] = []

    async def ok(settings, out_dir, industry="互联网", topic_title=None, **kw):
        ran.append(topic_title)
        return SimpleNamespace(duration_total=1.0)

    monkeypatch.setattr(cli_mod, "run_pipeline", ok)
    r = runner.invoke(app, ["batch", "-n", "2"])
    assert r.exit_code == 0, r.output
    assert ran == ["好题"] and "跳过 1 条" in r.output
    ran.clear()
    r = runner.invoke(app, ["batch", "-n", "2", "--retry-failed"])
    assert ran == ["坏题", "好题"]


@pytest.mark.parametrize("make_exc", [
    lambda: __import__("interview_video.tts", fromlist=["x"]).TTSAuthError("额度用完"),
    lambda: __import__("interview_video.llm_audit", fromlist=["x"]).LLMBudgetExceededError("超预算"),
    lambda: RuntimeError("[script] LLM 鉴权/配置错误, 不重试"),
])
def test_batch_aborts_on_fatal(monkeypatch, tmp_path: Path, make_exc) -> None:
    from interview_video.llm import AuthenticationError

    s = _settings(monkeypatch, tmp_path)
    backlog_mod.add_topics(s, [Topic(title=f"题{i}", industry="互联网") for i in range(3)])
    ran: list[str] = []

    async def boom(settings, out_dir, industry="互联网", topic_title=None, **kw):
        ran.append(topic_title)
        exc = make_exc()
        if type(exc) is RuntimeError:
            raise exc from AuthenticationError()
        raise exc

    monkeypatch.setattr(cli_mod, "run_pipeline", boom)
    r = runner.invoke(app, ["batch", "-n", "3"])
    assert r.exit_code == 1
    assert ran == ["题0"] and "批量已中止" in r.output
    assert backlog_mod.load_backlog(s)[0].attempts == 0  # 鉴权/预算不计选题失败次数


# ---------- 上传与定时发布 ----------

def test_batch_upload_assigns_slots(monkeypatch, tmp_path: Path) -> None:
    from interview_video import bilibili as bili_mod

    s = _settings(
        monkeypatch, tmp_path, bili_upload_enabled=True,
        bili_sessdata="a", bili_jct="b", bili_buvid3="c",
    )
    backlog_mod.add_topics(s, [Topic(title=f"题{i}", industry="互联网") for i in range(3)])
    start = date.today() + timedelta(days=2)
    # 已有一个项目占用了 start 当天 20:00
    other = s.out_root / "old"
    other.mkdir(parents=True)
    (other / "upload.json").write_text(json.dumps({"bvid": "BV0", "dtime": _ts(start, 20)}), encoding="utf-8")

    async def ok(settings, out_dir, industry="互联网", topic_title=None, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(duration_total=1.0)

    uploaded: list[tuple[Path, int | None]] = []

    async def fake_upload(project_dir, settings, *, force=False, dtime=None):
        uploaded.append((project_dir, dtime))
        bili_mod.write_upload_record(project_dir, f"BV{len(uploaded)}", dtime)
        return f"BV{len(uploaded)}"

    monkeypatch.setattr(cli_mod, "run_pipeline", ok)
    monkeypatch.setattr(cli_mod, "_default_out_dir", lambda st, sf, t, ind: st.out_root / t)
    monkeypatch.setattr(bili_mod, "upload_project", fake_upload)
    r = runner.invoke(app, [
        "batch", "-n", "3", "--upload", "--slots", "20:00,12:00", "--start", start.isoformat(),
    ])
    assert r.exit_code == 0, r.output
    assert [d for _, d in uploaded] == [
        _ts(start, 12), _ts(start + timedelta(days=1), 12), _ts(start + timedelta(days=1), 20),
    ]


def test_batch_upload_cookie_error_aborts(monkeypatch, tmp_path: Path) -> None:
    from interview_video import bilibili as bili_mod

    s = _settings(
        monkeypatch, tmp_path, bili_upload_enabled=True,
        bili_sessdata="a", bili_jct="b", bili_buvid3="c",
    )
    backlog_mod.add_topics(s, [Topic(title=f"题{i}", industry="互联网") for i in range(2)])
    ran: list[str] = []

    async def ok(settings, out_dir, industry="互联网", topic_title=None, **kw):
        ran.append(topic_title)
        return SimpleNamespace(duration_total=1.0)

    async def expired(project_dir, settings, *, force=False, dtime=None):
        raise bili_mod.BiliUploadError(bili_mod._COOKIE_EXPIRED)

    monkeypatch.setattr(cli_mod, "run_pipeline", ok)
    monkeypatch.setattr(bili_mod, "upload_project", expired)
    r = runner.invoke(app, ["batch", "-n", "2", "--upload"])
    assert r.exit_code == 1 and ran == ["题0"] and "批量已中止" in r.output


def test_batch_option_validation(monkeypatch, tmp_path: Path) -> None:
    _settings(monkeypatch, tmp_path)
    r = runner.invoke(app, ["batch", "--slots", "20:00"])
    assert r.exit_code == 2 and "--upload" in r.output
    r = runner.invoke(app, ["batch", "--upload", "--slots", "25:00"])
    assert r.exit_code == 2 and "--slots" in r.output
    r = runner.invoke(app, ["batch", "--upload", "--slots", "20:00", "--start", "10/01"])
    assert r.exit_code == 2 and "--start" in r.output
    r = runner.invoke(app, ["batch", "--upload"])  # 未开启上传
    assert r.exit_code == 2 and "BILI_UPLOAD_ENABLED" in r.output


def test_batch_refill(monkeypatch, tmp_path: Path) -> None:
    from interview_video import llm as llm_mod
    from interview_video.chains import topic as topic_mod

    s = _settings(monkeypatch, tmp_path)
    backlog_mod.add_topics(s, [Topic(title="已有", industry="护理")])
    seen: dict = {}

    async def fake_topics(llm, industry, n=5, avoid=None):
        seen.update(industry=industry, n=n, avoid=avoid)
        return [Topic(title=f"新{i}", industry=industry) for i in range(n)]

    ran: list[str] = []

    async def ok(settings, out_dir, industry="互联网", topic_title=None, **kw):
        ran.append(topic_title)
        return SimpleNamespace(duration_total=1.0)

    monkeypatch.setattr(llm_mod, "get_chat_model", lambda settings, audit=None: object())
    monkeypatch.setattr(topic_mod, "generate_topics", fake_topics)
    monkeypatch.setattr(cli_mod, "run_pipeline", ok)
    r = runner.invoke(app, ["batch", "-i", "护理", "-n", "2", "--refill", "3"])
    assert r.exit_code == 0, r.output
    assert seen == {"industry": "护理", "n": 2, "avoid": ["已有"]}
    assert ran == ["已有", "新0"]
