"""配置: pydantic-settings, 环境变量 = 字段名大写(与 drivecast 命名一致)。"""
from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- LLM(OpenAI 兼容) ----
    llm_model: str = "deepseek-chat"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_timeout: int = 300
    llm_temperature: float = 0.7

    # ---- TTS ----
    tts_provider: str = "edge"  # edge(免费,词级时间轴) | doubao(火山引擎,估算时间轴)
    tts_voice: str = "zh-CN-YunxiNeural"
    tts_rate: str = "+6%"

    # ---- 豆包 TTS(TTS_PROVIDER=doubao 时生效, seed-tts-2.0 HTTP 单向流式) ----
    tts_doubao_api_key: SecretStr | None = None   # 新版控制台一个 key 即可
    tts_doubao_app_key: str | None = None          # 旧版控制台退回 App ID + Access Key 一对
    tts_doubao_access_key: str | None = None
    tts_doubao_http_url: str = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    tts_doubao_resource_id: str = "seed-tts-2.0"   # 2.0 音色配 seed-tts-2.0, 1.0 音色配 seed-tts-1.0
    tts_doubao_uid: str = "interview-video-bot"
    tts_doubao_format: str = "mp3"
    tts_doubao_sample_rate: int = 24000
    tts_doubao_voice: str = "zh_male_m191_uranus_bigtts"  # 云舟(男); 女声 zh_female_vv_uranus_bigtts
    tts_doubao_rate: int = 0                              # speech_rate, [-50,100], 0 为原速
    tts_doubao_recv_timeout: int = 30                     # 流式单次读超时(秒)

    # ---- 内容 ----
    brand: str = "面试充电站"
    target_duration_sec: int = 100

    # ---- 视频 ----
    video_width: int = 1920
    video_height: int = 1080
    fps: int = 30

    # ---- 字幕(Pillow 字幕条) ----
    subtitle_fontsize: int = 52
    subtitle_bottom: int = 96          # 字幕条距底边像素
    subtitle_box: bool = True          # 半透明底条
    subtitle_font_path: str | None = None  # None=自动探测系统中文字体

    # ---- 响度 ----
    loudness_i: float = -16.0
    loudness_tp: float = -1.5
    loudness_lra: float = 11.0

    # ---- B站 ----
    bili_tid: int = 36
    bili_upload_enabled: bool = False
    bili_sessdata: str | None = None
    bili_jct: str | None = None
    bili_buvid3: str | None = None

    # ---- 输出 ----
    out_root: Path = Path("output")


def load_settings(env_file: str | None = None) -> Settings:
    if env_file:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    return Settings()
