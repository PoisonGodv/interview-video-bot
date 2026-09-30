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
# 超长字幕条细分时可断的位置(次级标点, 含半角; 不含 "." 以免拆开 3.5 之类数字)
_SOFT_BREAK_PUNCT = _SENTENCE_END_PUNCT + "、,;!?）)》」”"

# edge 词级 cue 对齐原文时: 词后紧跟的这些字符拼进该词; 词前的开括号/引号拼到词首
_ATTACH_PUNCT = "，。！？、；：,.!?;:…）)》」』”’"
_OPEN_PUNCT = "（(《「『“‘"
# 某词对不上原文时, 向后找回同步点的窗口(字符数)
_RESYNC_WINDOW = 30
# merge_word_cues 断点: 句末标点必断; 逗号类在当前行 ≥ max_chars*0.5 时断
_HARD_BREAK_PUNCT = "。！？!?"
_COMMA_BREAK_PUNCT = "，；：,;:"
# 判断断点时先剥掉行尾的闭引号/闭括号: "修好了。”" 仍算句末
_CLOSE_PUNCT = "”’」』）)》"


def _ends_with(text: str, puncts: str) -> bool:
    return text.rstrip().rstrip(_CLOSE_PUNCT).endswith(tuple(puncts))

# 字幕显示规则: 行尾去掉这些标点; 行中 "，" 换成空格; "。！？" 等保留
_DISPLAY_STRIP_END = "，、；：,;:"
_DISPLAY_MID_SPACE = "，"

# 失败重试: 2 次重试(共 3 次尝试), 指数退避基数(秒)
_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0

# 兜底时长: 最后 cue 结束后再留的余量(秒), 避免画面切太早
_TAIL_PAD = 0.4
# 字幕时间轴覆盖音频时长低于该比例时视为不可信, 改用字数估算
_MIN_CUE_COVERAGE = 0.8


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


def split_long_text(text: str, max_chars: int = 16) -> list[str]:
    """超过 max_chars 的一段字幕 → 多段: 优先在次级标点(、,; 等)后断, 找不到再硬切。

    断点取 max_chars 以内最靠后的标点(且切出的前段不短于 1/3, 避免碎片); 标点随前段。
    """
    pieces: list[str] = []
    rest = text
    while len(rest) > max_chars:
        cut = 0
        for i in range(max_chars, max(max_chars // 3, 1) - 1, -1):
            if rest[i - 1] in _SOFT_BREAK_PUNCT:
                cut = i
                break
        if not cut:
            cut = max_chars
            if rest[cut] in _SOFT_BREAK_PUNCT:
                cut += 1  # 不让下一段以标点开头
            else:
                # 不在英文单词/数字中间硬切: 退到该词开头(词过长则照切)
                j = cut
                while j > 0 and rest[j - 1].isascii() and rest[j - 1].isalnum():
                    j -= 1
                if rest[cut].isascii() and rest[cut].isalnum() and j >= max(max_chars // 3, 1):
                    cut = j
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest.strip():
        pieces.append(rest)
    elif pieces and rest:
        pieces[-1] += rest
    return pieces


def split_long_cues(cues: list[Cue], max_chars: int = 16) -> list[Cue]:
    """把超过 max_chars 的字幕条按 split_long_text 细分, 时间按字数比例分配。"""
    out: list[Cue] = []
    for c in cues:
        pieces = split_long_text(c.text, max_chars) if len(c.text) > max_chars else [c.text]
        if len(pieces) <= 1:
            out.append(c)
            continue
        total = sum(len(p) for p in pieces)
        t = c.start
        for i, p in enumerate(pieces):
            end = c.end if i == len(pieces) - 1 else t + (c.end - c.start) * len(p) / total
            out.append(Cue(start=round(t, 3), end=round(end, 3), text=p))
            t = end
    return out


def _split_subtitle_units(text: str, max_chars: int = 16) -> list[str]:
    """文本 → 字幕条文本单元: 句读标点处断(标点随前句), 超长再按次级标点/硬切细分。"""
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
        units += split_long_text(s, max_chars)
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

    return estimate_cues(text, ffprobe_duration(mp3_path), settings.subtitle_max_chars)


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
    return merge_word_cues(align_punctuation(words, text))


def _match_at(source: str, pos: int, word: str) -> int | None:
    """从 source[pos] 起逐字匹配 word(跳过空白, 大小写不敏感), 成功返回匹配末尾下标。"""
    i, n = pos, len(source)
    for ch in word:
        if ch.isspace():
            continue
        while i < n and source[i].isspace():
            i += 1
        if i < n and source[i].casefold() == ch.casefold():
            i += 1
        else:
            return None
    return i


def align_punctuation(words: list[Cue], source: str) -> list[Cue]:
    """edge WordBoundary 词文本不带标点: 沿原文游标逐词对齐, 把词后紧跟的标点拼回词里。

    词前的开引号/括号拼到词首, 词后有空白则保留一个空格(英文词间距)。
    某词对不上时原样保留并在窗口内找回同步点; 词本身已带标点时幂等。
    """
    out: list[Cue] = []
    cursor, n = 0, len(source)
    for w in words:
        text = w.text.strip()
        if not text:
            out.append(w)
            continue
        start = cursor
        while start < n and (source[start].isspace() or source[start] in _OPEN_PUNCT):
            start += 1
        end = _match_at(source, start, text)
        if end is None:
            for s in range(cursor + 1, min(n, cursor + _RESYNC_WINDOW) + 1):
                end = _match_at(source, s, text)
                if end is not None:
                    start = s
                    break
        if end is None:
            out.append(w)
            continue
        lead_start = start
        while lead_start > cursor and (
            source[lead_start - 1] in _OPEN_PUNCT or source[lead_start - 1].isspace()
        ):
            lead_start -= 1
        lead = "".join(ch for ch in source[lead_start:start] if ch in _OPEN_PUNCT)
        j = end
        while j < n and source[j] in _ATTACH_PUNCT:
            j += 1
        tail = source[end:j]
        if j < n and source[j].isspace():
            tail += " "
        out.append(Cue(start=w.start, end=w.end, text=lead + text + tail))
        cursor = j
    return out


def merge_word_cues(
    words: list[Cue],
    max_chars: int = 16,
    max_gap: float = 0.6,
    max_dur: float = 4.0,
) -> list[Cue]:
    """词 cues → 字幕条 cues。

    断条规则(SPECS §3):
    - 词间 gap > max_gap 断(边界等于不断);
    - 当前条末词以句末标点(。！？)结尾必断; 以逗号类(，；：)结尾且当前条 ≥ max_chars*0.5 断;
    - 累计字数 > max_chars 或单条时长 > max_dur 时, 往回找条内最后一个标点处断, 找不到才硬切;
    - 空文本词跳过; 超长单词独占一条。
    """
    bars: list[Cue] = []
    current: list[Cue] = []
    min_head = max(max_chars // 4, 1)

    def _chars(ws: list[Cue]) -> int:
        return sum(len(x.text) for x in ws)

    def _emit(ws: list[Cue]) -> None:
        bars.append(
            Cue(start=ws[0].start, end=ws[-1].end, text="".join(x.text for x in ws).strip())
        )

    def _close() -> None:
        nonlocal current
        if current:
            _emit(current)
            current = []

    def _over(w: Cue) -> bool:
        return (
            _chars(current) + len(w.text) > max_chars
            or w.end - current[0].start > max_dur
        )

    def _split_back(w: Cue) -> None:
        """在条内最后一个标点处断开, 标点后的词留给下一条; 无标点时若 w 收尾本分句,
        把前一个词带到下一条(避免"技术|难题，"这种半截词组); 都不行则整条关闭。"""
        nonlocal current
        for k in range(len(current) - 1, 0, -1):
            if (
                current[k - 1].text.rstrip().endswith(tuple(_SOFT_BREAK_PUNCT))
                and _chars(current[:k]) >= min_head
            ):
                _emit(current[:k])
                current = current[k:]
                return
        if (
            w.text.rstrip().endswith(tuple(_SOFT_BREAK_PUNCT))
            and len(current) >= 2
            and _chars(current[:-1]) >= min_head
            and len(current[-1].text) + len(w.text) <= max_chars
        ):
            _emit(current[:-1])
            current = current[-1:]
            return
        _close()

    for w in words:
        if not w.text.strip():
            continue  # 空文本跳过
        if current:
            last = current[-1].text.rstrip()
            if w.start - current[-1].end > max_gap:
                _close()  # 词间停顿过大, 断条
            elif _ends_with(last, _HARD_BREAK_PUNCT):
                _close()  # 句末标点必断
            elif _ends_with(last, _COMMA_BREAK_PUNCT) and _chars(current) >= max_chars * 0.5:
                _close()  # 逗号类且当前条已够长
            elif _over(w):
                _split_back(w)
                if current and _over(w):
                    _close()
        current.append(w)
    _close()
    return bars


def display_text(text: str) -> str:
    """字幕显示文本: 行尾去掉 _DISPLAY_STRIP_END, 行中 "，" 换空格, 合并多余空白。幂等。"""
    t = text.strip().rstrip(_DISPLAY_STRIP_END + " ")
    for ch in _DISPLAY_MID_SPACE:
        t = t.replace(ch, " ")
    return " ".join(t.split())


def format_display_cues(cues: list[Cue]) -> list[Cue]:
    """按显示规则整理字幕条文本; 整理后为空的条丢弃。"""
    out: list[Cue] = []
    for c in cues:
        t = display_text(c.text)
        if t:
            out.append(Cue(start=c.start, end=c.end, text=t))
    return out


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
    duration = max(音频实际时长, 最后 cue end) + 0.4s(ffprobe 不可用时退回 cue end);
    cue 为空或覆盖不足音频 80% 时改用 estimate_cues 按字数估算字幕。
    落盘/渲染的 cue 文本已按 format_display_cues 整理(行尾逗号去掉、行中逗号换空格)。
    """
    scenes_dir.mkdir(parents=True, exist_ok=True)
    primary_key = _voice_key(settings.tts_provider, settings)
    media: list[SceneMedia] = []
    for i, scene in enumerate(script.scenes):
        mp3_path = scenes_dir / f"scene_{i:03d}.mp3"
        cached = _load_scene_cache(mp3_path, scene.narration, primary_key)
        if cached is not None:
            cues, duration = cached
            audio_dur = _probe_audio_duration(mp3_path)
            if audio_dur and audio_dur + _TAIL_PAD > duration:
                # 旧缓存 duration 只按 cue 末尾算, 可能比音频短(成片截断口播), 按音频修正
                duration = _scene_duration(cues, audio_dur)
            cues = format_display_cues(cues)  # 旧缓存可能未整理, 幂等
            logger.info("场景 %d TTS 命中缓存, 跳过合成", i)
        else:
            cues, voice_key = await _synthesize_scene(i, scene.narration, mp3_path, settings)
            cues = split_long_cues(cues, settings.subtitle_max_chars)  # edge 超长单词/条再细分
            audio_dur = _probe_audio_duration(mp3_path)
            if audio_dur and (not cues or cues[-1].end < _MIN_CUE_COVERAGE * audio_dur):
                logger.warning(
                    "场景 %d 字幕时间轴仅覆盖 %.1fs / 音频 %.1fs, 改用按字数估算的字幕",
                    i, cues[-1].end if cues else 0.0, audio_dur,
                )
                cues = estimate_cues(scene.narration, audio_dur, settings.subtitle_max_chars)
            duration = _scene_duration(cues, audio_dur)
            cues = format_display_cues(cues)
            _save_scene_cache(mp3_path, scene.narration, cues, duration, voice_key)
        media.append(SceneMedia(index=i, mp3=mp3_path, cues=cues, duration=duration))
    return media


def _probe_audio_duration(mp3_path: Path) -> float | None:
    """ffprobe 读音频实际时长; ffprobe 缺失/文件不可解析时返回 None(调用方退回 cue 末尾)。"""
    from .assemble import ffprobe_duration  # 延迟导入, 避免模块级耦合

    try:
        dur = ffprobe_duration(mp3_path)
    except Exception as exc:  # noqa: BLE001 - 探测失败不阻断, 只失去校正
        logger.debug("ffprobe 读取 %s 时长失败: %s", mp3_path, exc)
        return None
    return dur if dur > 0 else None


def _scene_duration(cues: list[Cue], audio_dur: float | None) -> float:
    """场景时长 = max(音频时长, 最后 cue end) + 尾部留白, 保证 -t 截断不吃掉口播。"""
    last_end = cues[-1].end if cues else 0.0
    return max(audio_dur or 0.0, last_end) + _TAIL_PAD


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
