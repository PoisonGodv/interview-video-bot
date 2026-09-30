"""原生轻量异步工作流引擎 (workflow_engine.py) 单元测试。

验证内容:
1. 线性流水线与状态累加;
2. 条件路由 (同步/异步、dict 映射、sequence 校验);
3. 环路与双循环 (模拟分集/章节双循环);
4. 递归步数限制与无限循环保护;
5. 编译期拓扑检查与死链检测;
6. stream updates 与 values 两种流模式;
7. 同步 run_sync 与 stream_sync 接口;
8. 任务取消与异常透传。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from interview_video.workflow_engine import (
    END,
    START,
    CompiledWorkflow,
    RunContext,
    WorkflowEngine,
    WorkflowGraphError,
    WorkflowRecursionError,
    current_run_context,
)


@pytest.mark.asyncio
async def test_linear_workflow_run():
    """测试标准线性流水线及状态更新。"""
    g = WorkflowEngine(dict)

    async def step_a(state: dict[str, Any]) -> dict[str, Any]:
        return {"a": 1, "history": state.get("history", []) + ["a"]}

    async def step_b(state: dict[str, Any]) -> dict[str, Any]:
        return {"b": state["a"] + 2, "history": state["history"] + ["b"]}

    def step_c_sync(state: dict[str, Any]) -> dict[str, Any]:
        return {"c": state["b"] * 3, "history": state["history"] + ["c"]}

    g.add_node("a", step_a)
    g.add_node("b", step_b)
    g.add_node("c", step_c_sync)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", END)

    compiled = g.compile()
    assert isinstance(compiled, CompiledWorkflow)

    final = await compiled.run({"init": "hello"})
    assert final["init"] == "hello"
    assert final["a"] == 1
    assert final["b"] == 3
    assert final["c"] == 9
    assert final["history"] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_stream_updates_and_values():
    """测试 stream 的 updates 模式 (与 web.py 对齐) 和 values 模式。"""
    g = WorkflowEngine()

    async def n1(state: dict[str, Any]) -> dict[str, Any]:
        return {"s1": True}

    async def n2(state: dict[str, Any]) -> dict[str, Any]:
        return {"s2": True}

    g.add_node("node1", n1)
    g.add_node("node2", n2)
    g.set_entry_point("node1")
    g.add_edge("node1", "node2")
    g.set_finish_point("node2")

    compiled = g.compile()

    # 1. updates 模式: 逐节点生成 {node_name: update}
    chunks = []
    async for chunk in compiled.stream({"x": 0}, stream_mode="updates"):
        chunks.append(chunk)

    assert len(chunks) == 2
    assert chunks[0] == {"node1": {"s1": True}}
    assert chunks[1] == {"node2": {"s2": True}}

    # 2. values 模式: 逐节点输出当前累积的 full state
    val_chunks = []
    async for chunk in compiled.stream({"x": 0}, stream_mode="values"):
        val_chunks.append(chunk)

    assert len(val_chunks) == 2
    assert val_chunks[0] == {"x": 0, "s1": True}
    assert val_chunks[1] == {"x": 0, "s1": True, "s2": True}


@pytest.mark.asyncio
async def test_conditional_edges_routing():
    """测试条件路由分支: 支持同步与异步 router, 支持 path_map 映射。"""
    g = WorkflowEngine()

    async def start_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"val": state["input"]}

    async def branch_even(state: dict[str, Any]) -> dict[str, Any]:
        return {"result": "even"}

    def branch_odd(state: dict[str, Any]) -> dict[str, Any]:
        return {"result": "odd"}

    def router(state: dict[str, Any]) -> str:
        return "route_even" if state["val"] % 2 == 0 else "route_odd"

    g.add_node("start", start_node)
    g.add_node("even", branch_even)
    g.add_node("odd", branch_odd)

    g.add_edge(START, "start")
    g.add_conditional_edges(
        "start",
        router,
        path_map={"route_even": "even", "route_odd": "odd"},
    )
    g.add_edge("even", END)
    g.add_edge("odd", END)

    compiled = g.compile()

    res1 = await compiled.run({"input": 4})
    assert res1["result"] == "even"

    res2 = await compiled.run({"input": 7})
    assert res2["result"] == "odd"


@pytest.mark.asyncio
async def test_double_loop_state_machine():
    """测试模拟 series.py 的双循环结构 (外层集循环 + 内层章节循环)。"""
    # 结构:
    # plan -> setup -(还有集?)-> outline -> chapter -(还有章?)-> chapter
    #           ^                                     | (无章)
    #           |------- build_script <---------------|
    #           | (无集) -> END
    g = WorkflowEngine()

    async def plan_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"episodes": [2, 3], "ep_idx": 0, "completed": []}

    async def setup_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"ch_idx": 0}

    async def outline_node(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "outline_ready": True,
            "ch_count": state["episodes"][state["ep_idx"]],
        }

    async def chapter_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"ch_idx": state["ch_idx"] + 1}

    async def build_script_node(state: dict[str, Any]) -> dict[str, Any]:
        ep_no = state["ep_idx"] + 1
        return {
            "completed": state["completed"] + [f"ep{ep_no}"],
            "ep_idx": state["ep_idx"] + 1,
        }

    def has_episode(state: dict[str, Any]) -> str:
        return "outline" if state["ep_idx"] < len(state["episodes"]) else END

    def has_chapter(state: dict[str, Any]) -> str:
        return "chapter" if state["ch_idx"] < state["ch_count"] else "build_script"

    g.add_node("plan", plan_node)
    g.add_node("setup", setup_node)
    g.add_node("outline", outline_node)
    g.add_node("chapter", chapter_node)
    g.add_node("build_script", build_script_node)

    g.add_edge(START, "plan")
    g.add_edge("plan", "setup")
    g.add_conditional_edges("setup", has_episode)
    g.add_edge("outline", "chapter")
    g.add_conditional_edges("chapter", has_chapter)
    g.add_edge("build_script", "setup")

    compiled = g.compile()
    final = await compiled.run({})
    # 两集分别为 2 章与 3 章
    assert final["completed"] == ["ep1", "ep2"]
    assert final["ep_idx"] == 2


@pytest.mark.asyncio
async def test_recursion_limit_protection():
    """测试无限循环受到 max_steps / RunContext.recursion_limit 保护。"""
    g = WorkflowEngine()

    async def loop_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"count": state.get("count", 0) + 1}

    g.add_node("loop", loop_node)
    g.add_edge(START, "loop")
    g.add_edge("loop", "loop")  # 显式自环死循环

    compiled = g.compile()

    with pytest.raises(WorkflowRecursionError) as exc_info:
        await compiled.run({}, context=RunContext(recursion_limit=15))

    assert "工作流步数达到上限 15" in str(exc_info.value)


def test_compile_validation_errors():
    """测试编译期图拓扑校验。"""
    # 1. 未指定起始节点
    g1 = WorkflowEngine()
    g1.add_node("n1", lambda s: {})
    with pytest.raises(WorkflowGraphError, match="未指定起始节点"):
        g1.compile()

    # 2. 起始节点不存在
    g2 = WorkflowEngine()
    g2.set_entry_point("non_existent")
    with pytest.raises(WorkflowGraphError, match="未在图中注册"):
        g2.compile()

    # 3. 边指向未注册节点
    g3 = WorkflowEngine()
    g3.add_node("n1", lambda s: {})
    g3.add_edge(START, "n1")
    g3.add_edge("n1", "missing_node")
    with pytest.raises(WorkflowGraphError, match="终点 'missing_node' 未在图中注册"):
        g3.compile()

    # 4. 重复添加节点
    g4 = WorkflowEngine()
    g4.add_node("dup", lambda s: {})
    with pytest.raises(ValueError, match="已存在"):
        g4.add_node("dup", lambda s: {})

    # 5. 保留名称校验
    with pytest.raises(ValueError, match="不可使用保留关键字"):
        g4.add_node(START, lambda s: {})
    with pytest.raises(ValueError, match="不可使用保留关键字"):
        g4.add_node(END, lambda s: {})

    # 6. 同时添加无条件边和条件边
    g5 = WorkflowEngine()
    g5.add_node("n1", lambda s: {})
    g5.add_edge("n1", END)
    with pytest.raises(WorkflowGraphError, match="已定义无条件边"):
        g5.add_conditional_edges("n1", lambda s: END)


def test_sync_invoke_and_stream():
    """测试同步便捷方法 run_sync 和 stream_sync。"""
    g = WorkflowEngine()

    def add_ten(state: dict[str, Any]) -> dict[str, Any]:
        return {"val": state["val"] + 10}

    def double(state: dict[str, Any]) -> dict[str, Any]:
        return {"val": state["val"] * 2}

    g.add_node("add", add_ten)
    g.add_node("mul", double)
    g.add_edge(START, "add")
    g.add_edge("add", "mul")
    g.add_edge("mul", END)

    compiled = g.compile()

    final = compiled.run_sync({"val": 5})
    assert final["val"] == 30

    chunks = list(compiled.stream_sync({"val": 5}, stream_mode="updates"))
    assert len(chunks) == 2
    assert chunks[0] == {"add": {"val": 15}}
    assert chunks[1] == {"mul": {"val": 30}}


def test_get_graph_compatibility():
    """测试 get_graph() 结构与 nodes 字典契约。"""
    g = WorkflowEngine()
    g.add_node("step1", lambda s: {})
    g.add_node("step2", lambda s: {})
    g.add_edge(START, "step1")
    g.add_edge("step1", "step2")
    g.add_edge("step2", END)

    compiled = g.compile()
    graph_view = compiled.get_graph()

    assert set(graph_view.nodes.keys()) >= {"__start__", "step1", "step2", "__end__"}
    assert len(graph_view.edges) == 3


@pytest.mark.asyncio
async def test_node_receives_run_context():
    """节点按签名接收 (state, context); 执行期间 current_run_context() 可取到同一对象, 结束后复位。"""
    g = WorkflowEngine()
    seen: dict[str, Any] = {}

    async def node_with_context(state: dict[str, Any], context: RunContext) -> dict[str, Any]:
        seen["arg"] = context
        seen["ambient"] = current_run_context()
        return {"done": True}

    g.add_node("worker", node_with_context)
    g.add_edge(START, "worker")
    g.add_edge("worker", END)

    ctx = RunContext(extra={"user_tag": "admin_run"})
    final = await g.compile().run({}, context=ctx)
    assert final["done"] is True
    assert seen["arg"] is ctx and seen["ambient"] is ctx
    assert current_run_context() is None


@pytest.mark.asyncio
async def test_run_context_audit_reaches_llm_client(tmp_path):
    """get_chat_model 未显式传 audit 时取当前 RunContext.audit。"""
    from interview_video.config import Settings
    from interview_video.llm import get_chat_model
    from interview_video.llm_audit import LLMCallAudit

    audit = LLMCallAudit(tmp_path)
    settings = Settings(_env_file=None, llm_api_key="sk", llm_fallback_model="fb")
    seen: dict[str, Any] = {}

    def node(state: dict[str, Any]) -> dict[str, Any]:
        client = get_chat_model(settings)
        seen["audit"] = client.audit
        seen["fallback_audit"] = client.fallbacks[0].audit
        return {}

    g = WorkflowEngine()
    g.add_node("n", node)
    g.add_edge(START, "n")
    await g.compile().run({}, context=RunContext(audit=audit))
    assert seen["audit"] is audit and seen["fallback_audit"] is audit
    assert get_chat_model(settings).audit is None


@pytest.mark.asyncio
async def test_workflow_cancellation():
    """测试工作流执行过程中 task.cancel() 能正常引发 CancelledError。"""
    g = WorkflowEngine()

    async def slow_node(state: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"done": True}

    g.add_node("slow", slow_node)
    g.add_edge(START, "slow")
    g.add_edge("slow", END)

    compiled = g.compile()
    task = asyncio.create_task(compiled.run({}))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

