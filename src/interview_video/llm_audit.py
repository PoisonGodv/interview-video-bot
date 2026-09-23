"""LLM 调用审计: 每次调用的 token/耗时/输入输出规模追加到 llm_calls.jsonl。

用法(流水线入口注入或客户端直接记录):

    audit = LLMCallAudit(out_dir)
    # 方式 1: 直接记录
    audit.record_call(model="deepseek-chat", elapsed_ms=350.2, prompt="...", response="...", ...)
    # 方式 2: 保持与原有生命周期钩子兼容
    audit.on_chat_model_start(..., run_id=run_id)
    audit.on_llm_end(..., run_id=run_id)

产物字段: ts / model / elapsed_ms / prompt_chars / response_chars /
input_tokens / output_tokens / total_tokens / error。
计费口径(doubao TTS 等非 LLM 调用不在其中)。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _cst_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class LLMCallAudit:
    """把每次 LLM 调用追加为一行 JSON(审计/成本核算, 不影响主流程)。"""

    def __init__(self, out_dir: Path) -> None:
        self.path = Path(out_dir) / "llm_calls.jsonl"
        self._starts: dict[Any, float] = {}
        self._prompts: dict[Any, str] = {}

    def record_call(
        self,
        *,
        model: str | None = None,
        elapsed_ms: float = 0.0,
        prompt: str = "",
        response: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        error: str | None = None,
    ) -> None:
        """原生直接记录一次 LLM 调用。"""
        record: dict[str, Any] = {
            "ts": _cst_now(),
            "model": model,
            "elapsed_ms": round(elapsed_ms, 1),
            "prompt_chars": len(prompt),
            "response_chars": len(response),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens or (input_tokens + output_tokens),
        }
        if error:
            record["error"] = error
        self._append(record)

    # ---- 保持兼容的原有回调接口 ----

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[Any]],
        *,
        run_id: Any,
        **kw: Any,
    ) -> None:
        self._starts[run_id] = time.perf_counter()
        try:
            flat = [m for batch in messages for m in batch]
            self._prompts[run_id] = "\n".join(
                getattr(m, "content", "") if isinstance(getattr(m, "content", ""), str)
                else (m.get("content", "") if isinstance(m, dict) else str(m))
                for m in flat
            )[-2000:]
        except Exception:  # noqa: BLE001
            self._prompts[run_id] = ""

    def on_llm_end(self, response: Any, *, run_id: Any, **kw: Any) -> None:
        elapsed_ms = (time.perf_counter() - self._starts.pop(run_id, time.perf_counter())) * 1000
        prompt = self._prompts.pop(run_id, "")
        usage = self._extract_usage(response)
        text = ""
        try:
            if hasattr(response, "generations"):
                gens = response.generations or []
                if gens and gens[0]:
                    text = getattr(gens[0][0], "text", "") or ""
            elif hasattr(response, "content"):
                text = str(response.content)
            elif isinstance(response, str):
                text = response
        except Exception:  # noqa: BLE001
            text = ""

        model_name = None
        if hasattr(response, "llm_output") and response.llm_output:
            model_name = response.llm_output.get("model_name")
        elif hasattr(response, "model"):
            model_name = response.model

        self._append({
            "ts": _cst_now(),
            "model": model_name,
            "elapsed_ms": round(elapsed_ms, 1),
            "prompt_chars": len(prompt),
            "response_chars": len(text),
            **usage,
        })

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kw: Any) -> None:
        self._starts.pop(run_id, None)
        self._prompts.pop(run_id, None)
        self._append({
            "ts": _cst_now(),
            "error": f"{type(error).__name__}: {error}",
        })

    # ---- 内部工具 ----

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, int]:
        try:
            if hasattr(response, "usage") and isinstance(response.usage, dict):
                u = response.usage
                return {
                    "input_tokens": u.get("prompt_tokens", 0) or u.get("input_tokens", 0),
                    "output_tokens": u.get("completion_tokens", 0) or u.get("output_tokens", 0),
                    "total_tokens": u.get("total_tokens", 0),
                }
            llm_output = getattr(response, "llm_output", None) or {}
            tu = llm_output.get("token_usage") or {}
            if tu:
                return {
                    "input_tokens": tu.get("prompt_tokens", 0),
                    "output_tokens": tu.get("completion_tokens", 0),
                    "total_tokens": tu.get("total_tokens", 0),
                }
            gens = (getattr(response, "generations", []) or [])[0]
            msg = getattr(gens[0], "message", None) if gens else None
            um = getattr(msg, "usage_metadata", None) or {}
            if um:
                return {
                    "input_tokens": um.get("input_tokens", 0),
                    "output_tokens": um.get("output_tokens", 0),
                    "total_tokens": um.get("total_tokens", 0),
                }
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _append(self, record: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.debug("llm_calls.jsonl 写入失败(忽略): %s", exc)


def summarize_usage(out_dir: Path) -> dict[str, int]:
    """汇总一次运行的 llm_calls.jsonl → token 总量(命令行/evals 汇报用)。"""
    path = Path(out_dir) / "llm_calls.jsonl"
    totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if not path.exists():
        return totals
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        totals["calls"] += 1
        for k in ("input_tokens", "output_tokens", "total_tokens"):
            totals[k] += int(rec.get(k) or 0)
    return totals
