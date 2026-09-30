"""LLM 调用审计: 每次调用的 token/耗时/输入输出规模追加到 llm_calls.jsonl。

用法: 工作流入口放进 RunContext(audit=LLMCallAudit(out_dir)), HTTP 客户端每次调用后

    audit.record_call(model="deepseek-chat", elapsed_ms=350.2, prompt="...", response="...", ...)

产物字段: ts / model / elapsed_ms / prompt_chars / response_chars /
input_tokens / output_tokens / total_tokens / error。
计费口径(doubao TTS 等非 LLM 调用不在其中)。
"""
from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
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

    def _append(self, record: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.debug("llm_calls.jsonl 写入失败(忽略): %s", exc)


class LLMBudgetExceededError(RuntimeError):
    """本次运行 LLM 调用次数/token 超过 LLM_MAX_CALLS_PER_RUN / LLM_MAX_TOKENS_PER_RUN。"""


class RunBudget:
    """单次运行的 LLM 硬上限计数器(进程内, 续跑重新计数)。"""

    def __init__(self, max_calls: int | None = None, max_tokens: int | None = None) -> None:
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.calls = 0
        self.tokens = 0

    def acquire(self) -> None:
        """每次发 HTTP 请求前调用: 已达上限即抛 LLMBudgetExceededError, 否则计一次调用。"""
        hint = "已完成的产物已落盘, 调大上限后可续跑(不重复付费)"
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise LLMBudgetExceededError(
                f"LLM 预算超限: 本次运行已调用 {self.calls} 次, "
                f"达到 LLM_MAX_CALLS_PER_RUN={self.max_calls}; {hint}"
            )
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            raise LLMBudgetExceededError(
                f"LLM 预算超限: 本次运行已用 {self.tokens} tokens, "
                f"达到 LLM_MAX_TOKENS_PER_RUN={self.max_tokens}; {hint}"
            )
        self.calls += 1

    def add_tokens(self, total_tokens: int) -> None:
        self.tokens += max(0, int(total_tokens or 0))


_BUDGET: contextvars.ContextVar[RunBudget | None] = contextvars.ContextVar(
    "llm_run_budget", default=None
)


def current_budget() -> RunBudget | None:
    return _BUDGET.get()


@contextmanager
def run_budget(settings: Any) -> Iterator[RunBudget | None]:
    """在一次运行(单集/系列/Web 任务)范围内启用 LLM 硬上限; 未配置上限则不计数。

    已在某个运行范围内(如系列内逐集跑流水线)时沿用外层计数器, 不重新计数。
    """
    outer = _BUDGET.get()
    if outer is not None:
        yield outer
        return
    max_calls = getattr(settings, "llm_max_calls_per_run", None)
    max_tokens = getattr(settings, "llm_max_tokens_per_run", None)
    budget = RunBudget(max_calls, max_tokens) if (max_calls or max_tokens) else None
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


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
