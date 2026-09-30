"""配置: pydantic-settings, 环境变量 = 字段名大写(与 drivecast 命名一致)。

.env 查找顺序(首个存在者生效): ``$IVB_ENV_FILE`` → ``./.env`` → ``~/.config/ivb/.env``。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, PrivateAttr, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- LLM(OpenAI 兼容) ----
    llm_model: str = "deepseek-chat"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_timeout: int = 300              # 单次请求读超时(秒), 长章节生成需要足够大
    llm_connect_timeout: float = 10.0   # 建连超时(秒), 与读超时分开, 网络不通时快速失败重试
    llm_temperature: float = 0.7
    llm_max_tokens: int | None = None   # None=用网关默认; 显式设置可防长输出截断
    llm_max_retries: int = 2            # 客户端层网络/限流重试次数
    # 备用模型(主模型限流/宕机时自动切换, chains 层零改动)
    llm_fallback_model: str | None = None
    llm_fallback_base_url: str | None = None
    llm_fallback_api_key: SecretStr | None = None
    # 单次运行(一集/一个系列/一个 Web 任务)的 LLM 硬上限; None=不限制。超限抛预算错误并停下, 可续跑
    llm_max_calls_per_run: int | None = Field(default=None, ge=1)
    llm_max_tokens_per_run: int | None = Field(default=None, ge=1)
    # 费用预估单价(可选, 币种随你的账单); 未配置时预估只显示调用次数/token/字符数
    llm_price_input_per_1k: float | None = Field(default=None, ge=0)
    llm_price_output_per_1k: float | None = Field(default=None, ge=0)
    tts_price_per_10k_chars: float | None = Field(default=None, ge=0)

    # ---- TTS ----
    tts_provider: Literal["edge", "doubao"] = "edge"  # 拼错直接配置报错, 不再静默回退
    tts_voice: str = "zh-CN-YunxiNeural"
    tts_rate: str = "+6%"
    # 主通道重试耗尽/鉴权失败时的备用通道; None=不回退(常用 doubao→edge, edge 免凭证)
    tts_fallback_provider: Literal["edge", "doubao"] | None = None

    # ---- 豆包 TTS(TTS_PROVIDER=doubao 时生效, seed-tts-2.0 HTTP 单向流式) ----
    tts_doubao_api_key: SecretStr | None = None   # 新版控制台一个 key 即可
    tts_doubao_app_key: str | None = None          # 旧版控制台退回 App ID + Access Key 一对
    tts_doubao_access_key: SecretStr | None = None
    tts_doubao_http_url: str = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    tts_doubao_resource_id: str = "seed-tts-2.0"   # 2.0 音色配 seed-tts-2.0, 1.0 音色配 seed-tts-1.0
    tts_doubao_uid: str = "interview-video-bot"
    tts_doubao_format: str = "mp3"
    tts_doubao_sample_rate: int = 24000
    tts_doubao_voice: str = "zh_male_m191_uranus_bigtts"  # 云舟(男); 女声 zh_female_vv_uranus_bigtts
    tts_doubao_rate: int = Field(default=0, ge=-50, le=100)  # speech_rate, 0 为原速
    tts_doubao_recv_timeout: int = 30                     # 流式单次读超时(秒)

    # ---- 内容 ----
    brand: str = "面试充电站"
    target_duration_sec: int = 100

    # ---- 系列长视频 ----
    series_review: bool = True       # 审稿 agent 开关(关掉每章省一次 LLM 调用)
    series_chapter_sec: int = 150    # 章节目标时长(秒), 即上下文切分粒度

    # ---- 视频 ----
    video_aspect: Literal["16:9", "9:16"] = "16:9"  # 9:16 竖屏(抖音/B站竖屏), 画布自动转成 1080x1920
    video_width: int = 1920
    video_height: int = 1080
    fps: int = 30

    # ---- 背景音乐(可选) ----
    bgm_file: Path | None = None       # 未设置=不加 BGM; 循环铺满全片, 结尾淡出
    bgm_volume_db: float = Field(default=-18.0, le=0)
    bgm_ducking: bool = True           # 口播时自动压低 BGM(sidechaincompress)

    # ---- 字幕(Pillow 字幕条) ----
    subtitle_fontsize: int = 52
    subtitle_bottom: int = 96          # 字幕条距底边像素
    subtitle_box: bool = True          # 半透明底条
    subtitle_font_path: str | None = None  # None=自动探测系统中文字体
    subtitle_max_chars: int = Field(default=16, ge=6, le=16)  # 单条字幕最多字数(≤16), 超长优先在标点处细分

    # ---- 响度 ----
    loudness_i: float = -16.0
    loudness_tp: float = -1.5
    loudness_lra: float = 11.0

    # ---- B站 ----
    bili_tid: int = 36
    bili_upload_enabled: bool = False
    bili_sessdata: SecretStr | None = None
    bili_jct: SecretStr | None = None
    bili_buvid3: SecretStr | None = None

    # ---- 输出 ----
    out_root: Path = Path("output")  # 相对路径按当前目录解析; 可写绝对路径或 ~/...

    # ---- Web 操作台 ----
    # 写操作令牌(请求头 X-IVB-Token); 未设置时: 绑回环不校验, 绑非回环地址启动时自动生成并打印
    web_token: SecretStr | None = None
    # 额外允许的 Host 头(逗号分隔, 如反代域名); 回环名与 --host 本身总是允许
    web_allowed_hosts: str | None = None

    @field_validator(
        "llm_api_key", "llm_base_url", "llm_max_tokens",
        "llm_fallback_model", "llm_fallback_base_url", "llm_fallback_api_key",
        "llm_max_calls_per_run", "llm_max_tokens_per_run",
        "llm_price_input_per_1k", "llm_price_output_per_1k", "tts_price_per_10k_chars",
        "tts_fallback_provider", "tts_doubao_api_key", "tts_doubao_app_key", "tts_doubao_access_key",
        "subtitle_font_path", "bgm_file", "bili_sessdata", "bili_jct", "bili_buvid3",
        "web_token", "web_allowed_hosts",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        """空值/误把行内注释当值(``KEY=   # 说明``) → None。"""
        if isinstance(v, SecretStr):
            v = v.get_secret_value()
        if isinstance(v, str):
            v = v.strip()
            if not v or v.startswith("#"):
                return None
        return v

    _env_path: Path | None = PrivateAttr(default=None)  # load_settings 实际读取的 .env

    @field_validator("out_root", mode="after")
    @classmethod
    def _expand_out_root(cls, v: Path) -> Path:
        return v.expanduser()

    @field_validator("bgm_file", mode="after")
    @classmethod
    def _expand_bgm(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else None

    @property
    def portrait(self) -> bool:
        return self.video_aspect == "9:16"

    def frame_size(self) -> tuple[int, int]:
        """实际画布(宽, 高): 竖屏时把横向的 VIDEO_WIDTH/HEIGHT 对调(默认 1080x1920)。"""
        w, h = self.video_width, self.video_height
        if self.portrait == (w > h):
            w, h = h, w
        return w, h

    @field_validator("llm_base_url", "llm_fallback_base_url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError(f"必须以 http:// 或 https:// 开头, 当前: {v!r}")
        return v

    def llm_ready(self) -> bool:
        return self.llm_api_key is not None and bool(self.llm_api_key.get_secret_value())

    def doubao_ready(self) -> bool:
        if self.tts_doubao_api_key is not None:
            return True
        return bool(self.tts_doubao_app_key and self.tts_doubao_access_key)

    def bili_ready(self) -> bool:
        return all((self.bili_sessdata, self.bili_jct, self.bili_buvid3))


USER_ENV_FILE = Path("~/.config/ivb/.env")


def env_file_candidates() -> list[Path]:
    """按优先级列出 .env 候选: $IVB_ENV_FILE → ./.env → ~/.config/ivb/.env。"""
    out = []
    if explicit := os.environ.get("IVB_ENV_FILE", "").strip():
        out.append(Path(explicit).expanduser())
    out += [Path(".env"), USER_ENV_FILE.expanduser()]
    return out


def resolve_env_file() -> Path | None:
    """首个存在的候选(绝对路径); 都不存在返回 None(只用进程环境变量)。"""
    for p in env_file_candidates():
        if p.is_file():
            return p.resolve()
    return None


def load_settings(env_file: str | Path | None = None) -> Settings:
    path = Path(env_file).expanduser().resolve() if env_file else resolve_env_file()
    s = Settings(_env_file=str(path) if path else None)  # type: ignore[call-arg]
    s._env_path = path
    return s


def describe_paths(settings: Settings) -> str:
    """启动/日志用的一行: 实际读取的 .env 与解析后的输出目录。"""
    env = str(settings._env_path) if settings._env_path else "(未找到 .env, 仅用环境变量)"
    return f"配置 {env} · 输出 {settings.out_root.resolve()}"
