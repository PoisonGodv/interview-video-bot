"""环境自检: ``ivb doctor`` 全量检查 + 流水线启动前的廉价 preflight。

preflight 只查媒体硬依赖(ffmpeg/ffprobe/chromium/中文字体), 在花 LLM/TTS 钱之前失败。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError, Settings


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def check_binaries() -> list[Check]:
    out = []
    for name in ("ffmpeg", "ffprobe"):
        path = shutil.which(name)
        out.append(Check(
            name, path is not None,
            path or f"未找到 {name}; macOS: brew install ffmpeg, Debian/Ubuntu: apt install ffmpeg",
        ))
    return out


def check_chromium() -> Check:
    hint = "运行 playwright install chromium(或 make install)"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
    except Exception as exc:  # noqa: BLE001
        return Check("chromium", False, f"{type(exc).__name__}: {exc}; {hint}")
    if not exe.exists():
        return Check("chromium", False, f"浏览器未安装({exe}); {hint}")
    return Check("chromium", True, str(exe))


def check_font(settings: Settings) -> Check:
    from .subtitle_bars import load_subtitle_font

    try:
        font = load_subtitle_font(settings, 20)
    except RuntimeError as exc:
        return Check("中文字体", False, str(exc))
    return Check("中文字体", True, str(getattr(font, "path", "") or "已加载"))


def check_paths(settings: Settings) -> list[Check]:
    from .config import env_file_candidates

    env = settings._env_path
    tried = " / ".join(str(p) for p in env_file_candidates())
    out_root = settings.out_root.resolve()
    writable = os.access(out_root if out_root.exists() else _nearest_existing(out_root), os.W_OK)
    return [
        Check(".env", env is not None,
              str(env) if env is not None
              else f"未找到(依次查找 {tried}); 运行 ivb init 生成用户级配置"
                   f"{'并填写 LLM_API_KEY' if not settings.llm_ready() else ''}, 或设 IVB_ENV_FILE",
              required=False),
        Check("输出目录", writable,
              str(out_root) if writable else f"{out_root} 不可写; 改 OUT_ROOT(可用绝对路径)"),
    ]


def _nearest_existing(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


# 未配 LLM_API_KEY 时不可用的功能(doctor 列给用户看)
NEEDS_LLM = (
    "ivb topics", "ivb generate(不带 --sample/--script-file/--resume)", "ivb batch",
    "ivb series", "ivb eval", "B站元数据(标题/简介/标签)", "Web 生成选题/写脚本",
)


def check_llm_base_url(settings: Settings) -> Check:
    """DeepSeek 等非 OpenAI 模型却没配 base_url: 请求会打到 OpenAI 官方并 401/404。"""
    if settings.llm_base_url:
        return Check("LLM_BASE_URL", True, settings.llm_base_url, required=False)
    if settings.llm_model.lower().startswith("deepseek"):
        return Check(
            "LLM_BASE_URL", False,
            f"LLM_MODEL={settings.llm_model} 但 LLM_BASE_URL 为空(将请求 OpenAI 官方); "
            "DeepSeek 请设 LLM_BASE_URL=https://api.deepseek.com",
            required=False,
        )
    return Check("LLM_BASE_URL", True, "(默认 https://api.openai.com/v1)", required=False)


def check_config(settings: Settings) -> list[Check]:
    out = [
        *check_paths(settings),
        Check("LLM_API_KEY", settings.llm_ready(),
              f"模型 {settings.llm_model}" if settings.llm_ready()
              else "未配置; 以下功能不可用: " + "、".join(NEEDS_LLM)
                   + "; 免 key 可用 ivb generate --sample 冒烟", required=False),
        check_llm_base_url(settings),
    ]
    if settings.tts_provider == "doubao":
        out.append(Check(
            "豆包 TTS 凭证", settings.doubao_ready(),
            "已配置" if settings.doubao_ready()
            else "TTS_PROVIDER=doubao 但缺 TTS_DOUBAO_API_KEY(或 APP_KEY+ACCESS_KEY)",
        ))
    if settings.bgm_file is not None:
        bgm_ok = settings.bgm_file.is_file()
        out.append(Check(
            "BGM", bgm_ok,
            str(settings.bgm_file) if bgm_ok
            else f"BGM_FILE 不存在: {settings.bgm_file}(生成时将跳过背景音乐)",
            required=False,
        ))
    if settings.video_aspect == "9:16":
        w, h = settings.frame_size()
        out.append(Check("画幅", True, f"竖屏 9:16({w}x{h})", required=False))
    if settings.bili_upload_enabled:
        out.append(Check(
            "B站 cookie", settings.bili_ready(),
            "三件套齐全" if settings.bili_ready()
            else "BILI_UPLOAD_ENABLED=true 但 BILI_SESSDATA/BILI_JCT/BILI_BUVID3 不全",
            required=False,
        ))
        from .bilibili import tid_error, valid_tids

        err = tid_error(settings.bili_tid)
        out.append(Check(
            "B站分区", err is None,
            err or f"{settings.bili_tid}={valid_tids()[settings.bili_tid]}", required=False,
        ))
    return out


async def ping_llm(settings: Settings) -> Check:
    from .llm import get_chat_model

    try:
        llm = get_chat_model(settings)
        await llm.chat("ping, 只回复 ok", max_tokens=5)
    except Exception as exc:  # noqa: BLE001
        return Check("LLM 连通", False, f"{type(exc).__name__}: {exc}", required=False)
    return Check("LLM 连通", True, "ok", required=False)


def run_doctor(settings: Settings) -> list[Check]:
    return [*check_binaries(), check_chromium(), check_font(settings), *check_config(settings)]


def run_doctor_with_config_error(exc: ConfigError) -> list[Check]:
    """.env 校验失败时: 二进制/浏览器/字体照查(字体用默认配置探测), 每个坏字段一条 FAIL。"""
    where = str(exc.env_path) if exc.env_path else "(未找到 .env, 来自进程环境变量)"
    fallback = _default_settings()
    return [
        *check_binaries(), check_chromium(), check_font(fallback),
        *(Check("配置", False, f"{line}; 修改 {where}") for line in exc.lines),
    ]


def _default_settings() -> Settings:
    """不读 .env 与环境变量的纯默认配置(坏配置时给字体探测等用)。"""
    return Settings.model_construct()


def preflight(settings: Settings) -> None:
    """媒体硬依赖缺失时直接抛错(在调用 LLM/TTS 之前)。同步阻塞, async 调用方需放线程。"""
    failed = [c for c in (*check_binaries(), check_chromium(), check_font(settings)) if not c.ok]
    if failed:
        lines = "\n".join(f"- {c.name}: {c.detail}" for c in failed)
        raise RuntimeError(f"环境自检未通过(可运行 ivb doctor 查看详情):\n{lines}")
