"""TTS 配音模块(edge-tts / 火山豆包双通道)。

数据流: 每场景 narration → mp3 + 字幕条 cues(相对场景起点)
→ 全局时间轴由 offset_cues 按 SceneMedia.duration 累加平移得到。

时间轴来源按通道不同:
- edge:   stream() 的 WordBoundary chunk(词级, 100ns ticks) → merge_word_cues;
- doubao: seed-tts-2.0 单向流式不返回时间戳 → 按字数/标点权重把音频总时长
          比例分配到各字幕条(estimate_cues), 中文 TTS 语速均匀, 实测偏差可接受。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

import edge_tts

from .config import Settings
from .models import Cue, SceneMedia, Script

logger = logging.getLogger(__name__)

# WordBoundary 的 offset/duration 单位是 100ns ticks, 秒 = ticks / 1e7
_TICKS_PER_SECOND = 10_000_000.0

# 中文标点结尾优先断条(与 SPECS §3 一致)
_SENTENCE_END_PUNCT = "，。！？；："

# 失败重试: 2 次重试(共 3 次尝试), 指数退避基数(秒)
_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0

# 兜底时长: 最后 cue 结束后再留的余量(秒), 避免画面切太早
_TAIL_PAD = 0.4


# ---------------------------------------------------------------------------
# 豆包(火山引擎)通道: seed-tts-2.0 · HTTP 单向流式
# 接口: POST /api/v3/tts/unidirectional, NDJSON 逐行 {code, data(base64)},
# code=20000000 结束。协议与 drivecast drivecast/tts/doubao.py 一致。
# ---------------------------------------------------------------------------

_DOUBAO_STATUS_OK = 20000000
# 句读标点折算的停顿权重(单位: 字数), 用于估算时间轴
_PAUSE_WEIGHT = 1.5


class DoubaoTTSError(RuntimeError):
    """豆包 TTS 远端/协议错误, message 已含错误码与官方 message。"""


class TTSAuthError(DoubaoTTSError):
    """鉴权/权限/额度类错误: 重试无意义, 直接失败或走备用通道。"""


# 远端错误消息中提示鉴权/权限/额度问题的关键词(小写匹配)
_FATAL_HINTS = (
    "unauthorized", "forbidden", "invalid api key", "invalid access", "access denied",
    "not granted", "permission", "authenticat", "quota exceeded", "insufficient balance",
    "鉴权", "权限", "欠费", "余额不足",
)


def _require_doubao_credentials(settings: Settings) -> None:
    if settings.tts_doubao_api_key:
        return
    if settings.tts_doubao_app_key and settings.tts_doubao_access_key:
        return
    raise TTSAuthError(
        "豆包 TTS 缺少凭证: TTS_DOUBAO_API_KEY(新版控制台,推荐,一个 key 即可)"
        "或 TTS_DOUBAO_APP_KEY+TTS_DOUBAO_ACCESS_KEY(旧版);写入 .env"
    )


def _doubao_headers(settings: Settings) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": settings.tts_doubao_resource_id,
    }
    if settings.tts_doubao_api_key:
        headers["X-Api-Key"] = settings.tts_doubao_api_key.get_secret_value()
    else:
        headers["X-Api-App-Id"] = settings.tts_doubao_app_key or ""
        headers["X-Api-App-Key"] = settings.tts_doubao_app_key or ""  # 两版文档头名不一, 都带上
        headers["X-Api-Access-Key"] = (
            settings.tts_doubao_access_key.get_secret_value()
            if settings.tts_doubao_access_key else ""
        )
    return headers


def _doubao_payload(text: str, settings: Settings) -> dict:
    return {
        "user": {"uid": settings.tts_doubao_uid},
        "req_params": {
            "text": text,
            "speaker": settings.tts_doubao_voice,
            "audio_params": {
                "format": settings.tts_doubao_format,
                "sample_rate": settings.tts_doubao_sample_rate,
                # 范围校验由 Settings.tts_doubao_rate 的 Field(ge=-50, le=100) 保证
                "speech_rate": settings.tts_doubao_rate,
            },
        },
    }


def parse_ndjson_line(line: str) -> dict:
    """一行 NDJSON → dict; 坏行抛错(流协议, 吞掉会静默丢音频)。"""
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise DoubaoTTSError(f"NDJSON 坏行: {line[:200]}") from exc


async def _doubao_audio_bytes(text: str, settings: Settings) -> bytes:
    """POST 全文 → 流式收 NDJSON → 拼 base64 音频块。"""
    import httpx

    audio = bytearray()
    timeout = httpx.Timeout(10.0, read=settings.tts_doubao_recv_timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            settings.tts_doubao_http_url,
            headers=_doubao_headers(settings),
            json=_doubao_payload(text, settings),
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "ignore")
                if resp.status_code in (401, 403):
                    raise TTSAuthError(f"HTTP {resp.status_code}: {body[:300]}")
                raise DoubaoTTSError(f"HTTP {resp.status_code}: {body[:300]}")
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                item = parse_ndjson_line(line)
                code = item.get("code")
                if code == _DOUBAO_STATUS_OK:
                    break  # 合成结束
                if code not in (0, None):
                    raise DoubaoTTSError(f"status={code} {item.get('message', '')}")
                data = item.get("data")
                if data:
                    try:
                        audio += base64.b64decode(data)
                    except (ValueError, TypeError) as exc:
                        raise DoubaoTTSError("音频块 base64 解码失败") from exc
    if not audio:
        raise DoubaoTTSError("豆包 TTS 产物为空")
    return bytes(audio)


def _split_subtitle_units(text: str, max_chars: int = 16) -> list[str]:
    """文本 → 字幕条文本单元: 句读标点处断(标点随前句), 超长再按 max_chars 硬断。"""
    text = text.strip()
    if not text:
        return []
    sentences: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SENTENCE_END_PUNCT:
            sentences.append(buf)
            buf = ""
    if buf:
        sentences.append(buf)
    units: list[str] = []
    for s in sentences:
        while len(s) > max_chars:
            units.append(s[:max_chars])
            s = s[max_chars:]
        if s:
            units.append(s)
    return units


def estimate_cues(text: str, total_duration: float, max_chars: int = 16) -> list[Cue]:
    """按字数+标点停顿权重, 把音频总时长比例分配到各字幕条(豆包无时间戳用)。

    权重 = 字数 + 句读标点数 * _PAUSE_WEIGHT; 最后一条 end 精确对齐 total_duration。
    """
    units = _split_subtitle_units(text, max_chars)
    if not units or total_duration <= 0:
        return []
    weights = [
        len(u) + _PAUSE_WEIGHT * sum(1 for ch in u if ch in _SENTENCE_END_PUNCT)
        for u in units
    ]
    total_w = sum(weights)
    cues: list[Cue] = []
    t = 0.0
    for u, w in zip(units, weights):
        dur = total_duration * w / total_w
        cues.append(Cue(start=round(t, 3), end=round(t + dur, 3), text=u))
        t += dur
    cues[-1].end = round(total_duration, 3)  # 消除浮点累计误差
    return cues


async def synthesize_text_doubao(
    text: str, mp3_path: Path, settings: Settings
) -> list[Cue]:
    """豆包通道合成单段文本: 写 mp3, 返回估算时间轴的字幕条 cues。"""
    _require_doubao_credentials(settings)
    audio = await _doubao_audio_bytes(text, settings)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    mp3_path.write_bytes(audio)
    from .assemble import ffprobe_duration  # 延迟导入, 避免模块级耦合

    return estimate_cues(text, ffprobe_duration(mp3_path))


# ---------------------------------------------------------------------------
# edge-tts 通道(默认)
# ---------------------------------------------------------------------------


async def synthesize_text(
    text: str, mp3_path: Path, voice: str, rate: str
) -> list[Cue]:
    """合成单段文本: 写 mp3, 返回 merge 后的字幕条 cues(相对本段起点)。

    词级时间轴来自 stream() 的 WordBoundary chunk(offset/duration 为
    100ns ticks), 不经 SubMaker(其 get_srt 版本间不稳)。
    """
    communicate = edge_tts.Communicate(
        text, voice, rate=rate, boundary="WordBoundary"
    )
    words: list[Cue] = []
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    with open(mp3_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / _TICKS_PER_SECOND
                end = start + chunk["duration"] / _TICKS_PER_SECOND
                words.append(Cue(start=start, end=end, text=chunk["text"]))
    return merge_word_cues(words)


def merge_word_cues(
    words: list[Cue],
    max_chars: int = 16,
    max_gap: float = 0.6,
    max_dur: float = 4.0,
) -> list[Cue]:
    """词 cues → 字幕条 cues。

    断条规则(SPECS §3):
    - 累计字数 > max_chars 断;
    - 词间 gap > max_gap 断(边界等于不断);
    - 单条时长(首词 start → 末词 end) > max_dur 断;
    - 当前条末词以中文标点(，。！？；：)结尾, 优先断;
    - 空文本词跳过; 超长单词独占一条。
    """
    bars: list[Cue] = []
    current: list[Cue] = []

    def _close() -> None:
        nonlocal current
        if current:
            bars.append(
                Cue(
                    start=current[0].start,
                    end=current[-1].end,
                    text="".join(w.text for w in current),
                )
            )
            current = []

    for w in words:
        if not w.text.strip():
            continue  # 空文本跳过
        if current:
            gap = w.start - current[-1].end
            total_chars = sum(len(x.text) for x in current) + len(w.text)
            total_dur = w.end - current[0].start
            if gap > max_gap:
                _close()  # 词间停顿过大, 断条
            elif current[-1].text.endswith(tuple(_SENTENCE_END_PUNCT)):
                _close()  # 中文标点结尾优先断
            elif total_chars > max_chars:
                _close()  # 累计字数超限
            elif total_dur > max_dur:
                _close()  # 单条时长超限
        current.append(w)
    _close()
    return bars


def offset_cues(scene_media: list[SceneMedia]) -> list[Cue]:
    """把各场景 cues(相对场景起点)按 SceneMedia.duration 累加平移成全局时间轴。"""
    out: list[Cue] = []
    shift = 0.0
    for m in scene_media:
        for c in m.cues:
            out.append(Cue(start=c.start + shift, end=c.end + shift, text=c.text))
        shift += m.duration
    return out


def _fmt_srt_ts(t: float) -> str:
    """秒 → SRT 时间戳 HH:MM:SS,mmm。"""
    if t < 0:
        t = 0.0
    total_ms = int(round(t * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(cues: list[Cue]) -> str:
    """输出标准 SRT 文本(序号 + HH:MM:SS,mmm 时间戳), 便于人工核对。"""
    blocks = [
        f"{i}\n{_fmt_srt_ts(c.start)} --> {_fmt_srt_ts(c.end)}\n{c.text}"
        for i, c in enumerate(cues, start=1)
    ]
    return "\n\n".join(blocks) + "\n"


def _is_fatal_tts_error(err: Exception) -> bool:
    """鉴权/权限/额度类错误重试无意义, 直接失败(或走备用通道)。"""
    if isinstance(err, TTSAuthError):
        return True
    if isinstance(err, OSError):
        return False  # 本地文件错误(如 Permission denied)不是远端鉴权问题
    msg = str(err).lower()
    if "concurrency" in msg:
        return False  # 并发配额超限是瞬时的, 可重试
    return any(h in msg for h in _FATAL_HINTS)


def _voice_key(provider: str, settings: Settings) -> str:
    """缓存键的通道部分: 通道+音色+语速, 备用通道音频不会冒充主通道缓存。"""
    if provider == "doubao":
        return f"doubao:{settings.tts_doubao_voice}:{settings.tts_doubao_rate}"
    return f"edge:{settings.tts_voice}:{settings.tts_rate}"


async def _synthesize_once(
    provider: str, text: str, mp3_path: Path, settings: Settings
) -> list[Cue]:
    if provider == "doubao":
        return await synthesize_text_doubao(text, mp3_path, settings)
    return await synthesize_text(text, mp3_path, settings.tts_voice, settings.tts_rate)


async def _synthesize_with_retry(
    index: int, text: str, mp3_path: Path, settings: Settings, provider: str | None = None
) -> list[Cue]:
    """单场景单通道合成, 失败重试 2 次(指数退避); 鉴权类错误不重试。

    仍失败抛带场景序号的 RuntimeError(鉴权类为 TTSAuthError)。
    """
    provider = provider or settings.tts_provider
    last_err: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await _synthesize_once(provider, text, mp3_path, settings)
        except Exception as err:  # noqa: BLE001 —— 网络错误类型不可枚举, 统一重试
            last_err = err
            if _is_fatal_tts_error(err):
                logger.error("TTS[%s] 场景 %d 鉴权/额度错误, 不重试: %s", provider, index, err)
                raise TTSAuthError(
                    f"场景 {index} TTS[{provider}] 鉴权/权限/额度错误(不重试), "
                    f"请检查凭证与账户余额: {err}"
                ) from err
            if attempt < _MAX_ATTEMPTS:
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "TTS[%s] 场景 %d 第 %d/%d 次失败, %.1fs 后重试: %s",
                    provider, index, attempt, _MAX_ATTEMPTS, delay, err,
                )
                await asyncio.sleep(delay)
    raise RuntimeError(
        f"场景 {index} TTS[{provider}] 合成失败(已重试 {_MAX_ATTEMPTS - 1} 次): {last_err}"
    )


async def _synthesize_scene(
    index: int, text: str, mp3_path: Path, settings: Settings
) -> tuple[list[Cue], str]:
    """主通道合成, 失败且配了 TTS_FALLBACK_PROVIDER 时换备用通道; 返回 (cues, 实际通道缓存键)。"""
    primary = settings.tts_provider
    fallback = settings.tts_fallback_provider
    try:
        cues = await _synthesize_with_retry(index, text, mp3_path, settings, primary)
        return cues, _voice_key(primary, settings)
    except Exception as err:
        if not fallback or fallback == primary:
            raise
        logger.warning("TTS 场景 %d 主通道 %s 失败, 回退到 %s: %s", index, primary, fallback, err)
        try:
            cues = await _synthesize_with_retry(index, text, mp3_path, settings, fallback)
        except Exception as fb_err:
            raise RuntimeError(
                f"场景 {index} TTS 主通道 {primary} 与备用通道 {fallback} 均失败: "
                f"{err} | {fb_err}"
            ) from fb_err
        logger.warning("TTS 场景 %d 已由备用通道 %s 合成(音色与主通道不同)", index, fallback)
        return cues, _voice_key(fallback, settings)


async def synthesize_scenes(
    script: Script, scenes_dir: Path, settings: Settings
) -> list[SceneMedia]:
    """顺序合成所有场景(避免并发被限流), 产出 scene_%03d.mp3 与 merge 后 cues。

    幂等缓存: mp3 与旁车 scene_%03d.cues.json 都在、且口播文本指纹与
    通道/音色键一致时直接复用(备用通道产物不命中主通道, 下次重试主通道)(断点续跑/失败重试不重复付 TTS 费); 删掉 mp3 或改了文本会重合成。
    duration 兜底 = 最后 cue end + 0.4s; 无 cue 时仅留 0.4s。
    """
    scenes_dir.mkdir(parents=True, exist_ok=True)
    primary_key = _voice_key(settings.tts_provider, settings)
    media: list[SceneMedia] = []
    for i, scene in enumerate(script.scenes):
        mp3_path = scenes_dir / f"scene_{i:03d}.mp3"
        cached = _load_scene_cache(mp3_path, scene.narration, primary_key)
        if cached is not None:
            cues, duration = cached
            logger.info("场景 %d TTS 命中缓存, 跳过合成", i)
        else:
            cues, voice_key = await _synthesize_scene(i, scene.narration, mp3_path, settings)
            duration = cues[-1].end + _TAIL_PAD if cues else _TAIL_PAD
            _save_scene_cache(mp3_path, scene.narration, cues, duration, voice_key)
        media.append(SceneMedia(index=i, mp3=mp3_path, cues=cues, duration=duration))
    return media


# ---------------------------------------------------------------------------
# 幂等缓存(旁车 scene_XXX.cues.json: 口播指纹 + cues + duration)
# ---------------------------------------------------------------------------

def _cache_path(mp3_path: Path) -> Path:
    return mp3_path.with_suffix(".cues.json")


def _narration_fingerprint(narration: str) -> str:
    import hashlib

    return hashlib.sha1(narration.strip().encode("utf-8")).hexdigest()


def _save_scene_cache(
    mp3_path: Path, narration: str, cues: list[Cue], duration: float, voice_key: str
) -> None:
    try:
        _cache_path(mp3_path).write_text(
            json.dumps(
                {
                    "narration_sha1": _narration_fingerprint(narration),
                    "voice_key": voice_key,
                    "duration": duration,
                    "cues": [c.model_dump() for c in cues],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # 缓存写失败不影响本次合成, 只失去复用


def _load_scene_cache(
    mp3_path: Path, narration: str, voice_key: str
) -> tuple[list[Cue], float] | None:
    """mp3 + 旁车都在且口播指纹、通道/音色键一致 → (cues, duration); 否则 None。"""
    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        return None
    cache = _cache_path(mp3_path)
    if not cache.exists():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("narration_sha1") != _narration_fingerprint(narration):
            return None  # 文本变了, 缓存失效
        if data.get("voice_key", voice_key) != voice_key:
            return None  # 换了通道/音色, 或是备用通道产物(无此字段的旧旁车视为匹配)
        cues = [Cue.model_validate(c) for c in data.get("cues", [])]
        return cues, float(data.get("duration", 0.0))
    except (json.JSONDecodeError, ValueError, KeyError):
        return None
