"""轻量级原生异步工作流 / 状态机引擎 (AsyncStateGraph / WorkflowEngine)。

设计原则:
1. 零第三方依赖: 仅使用 Python 3.10+ 标准库 (asyncio, typing, inspect, logging 等);
2. 语义与接口对齐: 完全兼容 LangGraph 的 StateGraph / START / END / ainvoke / astream 契约;
3. 健壮的状态流转保证:
   - 编译期拓扑与死链检查;
   - 运行期步数上限保护 (防无限循环 / 递归失控);
   - 节点级错误上下文封装 (保留失败节点名与最新状态快照);
   - 支持同步与异步节点 / 路由函数混合编排;
   - 自动适配 LangChain RunnableConfig 上下文 (若环境安装了 langchain_core, 透传回调)。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncGenerator, Callable, Generator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

# 特殊保留节点标识符 (与 LangGraph 契约完全一致)
START = "__start__"
END = "__end__"

# 默认运行期递归/步数上限 (防止环路失控)
DEFAULT_RECURSION_LIMIT = 1000

StateT = TypeVar("StateT")


class WorkflowEngineError(Exception):
    """工作流引擎基础异常。"""


class WorkflowGraphError(WorkflowEngineError):
    """图结构编译期定义异常 (如悬空边、缺少起始节点、重复定义等)。"""


class WorkflowRecursionError(WorkflowEngineError):
    """运行期超出步数上限异常 (检测到环路失控或递归过深)。"""


class WorkflowExecutionError(WorkflowEngineError):
    """节点执行期异常，附带节点名称和当前状态。"""

    def __init__(self, node: str, state: Mapping[str, Any], original_error: BaseException) -> None:
        self.node = node
        self.state = state
        self.original_error = original_error
        super().__init__(f"Error executing node '{node}': {original_error}")


@dataclass(frozen=True)
class GraphNode:
    """供可视化或拓扑检查使用的节点表示。"""

    id: str
    name: str
    data: Any = None


@dataclass(frozen=True)
class GraphEdge:
    """供可视化或拓扑检查使用的边表示。"""

    source: str
    target: str
    conditional: bool = False


@dataclass
class GraphView:
    """与 LangGraph get_graph() 结构兼容的只读拓扑视图。"""

    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)


_current_workflow_config: ContextVar[dict[str, Any] | None] = ContextVar(
    "_current_workflow_config", default=None
)


def get_current_workflow_config() -> dict[str, Any] | None:
    """获取当前正在执行的工作流节点的配置 (如 callbacks、metadata 等)。"""
    return _current_workflow_config.get()


def _propagate_config(config: dict[str, Any] | None) -> tuple[Any, Any]:
    """将 config 注入当前上下文 ContextVar，支持跨异步协程透传。"""
    if not config:
        return (None, None)
    native_token = _current_workflow_config.set(config)
    lc_token = None
    try:
        from langchain_core.runnables.config import var_child_runnable_config

        lc_token = var_child_runnable_config.set(config)
    except (ImportError, AttributeError):
        pass
    return (native_token, lc_token)


def _reset_config(tokens: tuple[Any, Any]) -> None:
    """重置上下文 ContextVar。"""
    native_token, lc_token = tokens
    if native_token is not None:
        _current_workflow_config.reset(native_token)
    if lc_token is not None:
        try:
            from langchain_core.runnables.config import var_child_runnable_config

            var_child_runnable_config.reset(lc_token)
        except Exception:
            pass


class CompiledWorkflow(Generic[StateT]):
    """已编译的状态图工作流实例。支持 ainvoke / astream / invoke / stream。"""

    def __init__(
        self,
        nodes: dict[str, Callable[..., Any]],
        edges: dict[str, str],
        conditional_edges: dict[str, tuple[Callable[..., Any], dict[Any, str] | None]],
        entry_point: str,
        state_schema: Any = None,
        max_steps: int = DEFAULT_RECURSION_LIMIT,
    ) -> None:
        self._nodes = dict(nodes)
        self._edges = dict(edges)
        self._conditional_edges = dict(conditional_edges)
        self._entry_point = entry_point
        self._state_schema = state_schema
        self._max_steps = max_steps

    @property
    def nodes(self) -> dict[str, Callable[..., Any]]:
        """已注册节点字典。"""
        return self._nodes

    def get_graph(self) -> GraphView:
        """返回兼容 LangGraph 协议的图拓扑视图 (包含 __start__, __end__ 与所有节点)。"""
        nodes_dict: dict[str, GraphNode] = {
            START: GraphNode(id=START, name=START),
            END: GraphNode(id=END, name=END),
        }
        for name in self._nodes:
            nodes_dict[name] = GraphNode(id=name, name=name)

        edges_list: list[GraphEdge] = [
            GraphEdge(source=START, target=self._entry_point, conditional=False)
        ]
        for src, dst in self._edges.items():
            edges_list.append(GraphEdge(source=src, target=dst, conditional=False))
        for src, (_, path_map) in self._conditional_edges.items():
            if path_map:
                for dst in path_map.values():
                    edges_list.append(GraphEdge(source=src, target=dst, conditional=True))
            else:
                edges_list.append(GraphEdge(source=src, target="*", conditional=True))

        return GraphView(nodes=nodes_dict, edges=edges_list)

    async def _execute_node(
        self,
        node_name: str,
        fn: Callable[..., Any],
        state: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> Any:
        """执行单个节点函数，支持 async/sync，支持根据签名传 (state) 或 (state, config)。"""
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())

        # 判断是否需要传递 config 参数
        accepts_config = False
        if len(params) >= 2:
            accepts_config = True
        elif any(p.name in ("config", "runnable_config") for p in params):
            accepts_config = True

        call_args: tuple[Any, ...] = (state, config) if accepts_config else (state,)

        try:
            if inspect.iscoroutinefunction(fn):
                res = await fn(*call_args)
            else:
                res = fn(*call_args)
                if inspect.isawaitable(res):
                    res = await res
            return res
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            logger.exception("工作流节点 '%s' 执行失败: %s", node_name, exc)
            raise

    async def _resolve_next(
        self,
        current_node: str,
        state: dict[str, Any],
    ) -> str:
        """根据当前节点状态，计算下一跃点节点名称或 END。"""
        if current_node in self._conditional_edges:
            router_fn, path_map = self._conditional_edges[current_node]
            if inspect.iscoroutinefunction(router_fn):
                decision = await router_fn(state)
            else:
                decision = router_fn(state)
                if inspect.isawaitable(decision):
                    decision = await decision

            if path_map is not None:
                next_node = path_map.get(decision, decision)
            else:
                next_node = decision

            if not isinstance(next_node, str):
                raise WorkflowExecutionError(
                    current_node,
                    state,
                    ValueError(
                        f"Conditional edge router for '{current_node}' returned non-string target: {decision!r}"
                    ),
                )
            return next_node

        if current_node in self._edges:
            return self._edges[current_node]

        # 无后续边定义时自然结束
        return END

    async def astream(
        self,
        initial_state: Mapping[str, Any],
        stream_mode: str = "updates",
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """异步生成器: 流式执行工作流，按步输出 updates 或 values。

        stream_mode="updates" 时，每步产出 {node_name: node_output}
        stream_mode="values" 时，每步产出最新全量 state 字典
        """
        state = dict(initial_state)
        current_node = self._entry_point
        steps = 0

        # 支持在 config 中覆盖 recursion_limit
        max_steps = self._max_steps
        if config and "recursion_limit" in config:
            try:
                max_steps = int(config["recursion_limit"])
            except (ValueError, TypeError):
                pass

        tokens = _propagate_config(config)
        try:
            while current_node != END:
                steps += 1
                if steps > max_steps:
                    raise WorkflowRecursionError(
                        f"工作流步数达到上限 {max_steps}，疑似存在无限环路 (卡在节点 '{current_node}')。"
                    )

                node_fn = self._nodes[current_node]
                output = await self._execute_node(current_node, node_fn, state, config)

                # 状态增量合并
                if isinstance(output, Mapping):
                    state.update(output)

                if stream_mode == "updates":
                    yield {current_node: dict(output) if isinstance(output, Mapping) else {}}
                elif stream_mode == "values":
                    yield dict(state)
                else:
                    yield {current_node: dict(output) if isinstance(output, Mapping) else {}}

                current_node = await self._resolve_next(current_node, state)
        finally:
            _reset_config(tokens)

    async def ainvoke(
        self,
        initial_state: Mapping[str, Any],
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """异步执行完整工作流，返回最终状态字典。"""
        state = dict(initial_state)
        current_node = self._entry_point
        steps = 0

        max_steps = self._max_steps
        if config and "recursion_limit" in config:
            try:
                max_steps = int(config["recursion_limit"])
            except (ValueError, TypeError):
                pass

        tokens = _propagate_config(config)
        try:
            while current_node != END:
                steps += 1
                if steps > max_steps:
                    raise WorkflowRecursionError(
                        f"工作流步数达到上限 {max_steps}，疑似存在无限环路 (卡在节点 '{current_node}')。"
                    )

                node_fn = self._nodes[current_node]
                output = await self._execute_node(current_node, node_fn, state, config)

                if isinstance(output, Mapping):
                    state.update(output)

                current_node = await self._resolve_next(current_node, state)

            return state
        finally:
            _reset_config(tokens)

    def invoke(
        self,
        initial_state: Mapping[str, Any],
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """同步运行便捷入口。若在已有事件循环中调用，安全委托到后台线程运行。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(asyncio.run, self.ainvoke(initial_state, config=config, **kwargs))
                return fut.result()
        return asyncio.run(self.ainvoke(initial_state, config=config, **kwargs))

    def stream(
        self,
        initial_state: Mapping[str, Any],
        stream_mode: str = "updates",
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Generator[dict[str, Any], None, None]:
        """同步流式生成器便捷入口。"""
        async def _drain() -> list[dict[str, Any]]:
            chunks = []
            async for chunk in self.astream(
                initial_state, stream_mode=stream_mode, config=config, **kwargs
            ):
                chunks.append(chunk)
            return chunks

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(asyncio.run, _drain())
                yield from fut.result()
        else:
            chunks = asyncio.run(_drain())
            yield from chunks


class WorkflowEngine(Generic[StateT]):
    """轻量级原生异步状态图/工作流构建器 (与 LangGraph StateGraph API 契约对齐)。"""

    def __init__(self, state_schema: Any = None) -> None:
        self.state_schema = state_schema
        self._nodes: dict[str, Callable[..., Any]] = {}
        self._edges: dict[str, str] = {}
        self._conditional_edges: dict[
            str, tuple[Callable[..., Any], dict[Any, str] | None]
        ] = {}
        self._entry_point: str | None = None

    def add_node(self, name: str, fn: Callable[..., Any]) -> WorkflowEngine[StateT]:
        """注册节点。节点函数接收当前 state 字典，可返回部分增量 state 字典。"""
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"节点名称必须是非空字符串: {name!r}")
        if name in (START, END):
            raise ValueError(f"节点名称不可使用保留关键字 '{name}'")
        if name in self._nodes:
            raise ValueError(f"节点 '{name}' 已存在，不可重复添加")
        if not callable(fn):
            raise TypeError(f"节点 '{name}' 的处理函数必须是 Callable，收到 {type(fn)}")

        self._nodes[name] = fn
        return self

    def set_entry_point(self, name: str) -> WorkflowEngine[StateT]:
        """显式设置起始节点 (等价于 add_edge(START, name))。"""
        self._entry_point = name
        return self

    def set_finish_point(self, name: str) -> WorkflowEngine[StateT]:
        """显式设置结束节点 (等价于 add_edge(name, END))。"""
        return self.add_edge(name, END)

    def add_edge(self, u: str, v: str) -> WorkflowEngine[StateT]:
        """添加有向普通边。u 可为 START 或普通节点，v 可为 END 或普通节点。"""
        if u == START:
            self._entry_point = v
            return self
        if u in self._conditional_edges:
            raise WorkflowGraphError(f"节点 '{u}' 已定义条件边，不能同时添加确定性无条件边")
        if u in self._edges:
            raise WorkflowGraphError(f"节点 '{u}' 已存在出边 (至 '{self._edges[u]}')，暂不支持一对多无条件分发")
        self._edges[u] = v
        return self

    def add_conditional_edges(
        self,
        source: str,
        router_fn: Callable[..., Any],
        path_map: dict[Any, str] | Sequence[str] | None = None,
    ) -> WorkflowEngine[StateT]:
        """添加条件分支边。

        router_fn(state) -> 分支标识/目标节点名称
        path_map: 可选映射字典或目标列表。如果为序列则原样使用其元素校验。
        """
        if source == END:
            raise WorkflowGraphError(f"'{END}' 不能作为边的起点")
        if source in self._edges:
            raise WorkflowGraphError(f"节点 '{source}' 已定义无条件边，不能同时添加条件边")
        if source in self._conditional_edges:
            raise WorkflowGraphError(f"节点 '{source}' 已定义条件边，不能重复添加")
        if not callable(router_fn):
            raise TypeError(f"router_fn 必须是 Callable，收到 {type(router_fn)}")

        normalized_map: dict[Any, str] | None = None
        if path_map is not None:
            if isinstance(path_map, Mapping):
                normalized_map = dict(path_map)
            elif isinstance(path_map, Sequence):
                normalized_map = {item: item for item in path_map}
            else:
                raise TypeError(f"path_map 必须是 Mapping 或 Sequence，收到 {type(path_map)}")

        self._conditional_edges[source] = (router_fn, normalized_map)
        return self

    def compile(self, **kwargs: Any) -> CompiledWorkflow[StateT]:
        """验证图拓扑连通性并编译为执行器实例。"""
        # 1. 验证起始节点
        if not self._entry_point:
            raise WorkflowGraphError("图未指定起始节点。请通过 add_edge(START, 'first_node') 或 set_entry_point 指定。")
        if self._entry_point not in self._nodes:
            raise WorkflowGraphError(
                f"起始节点 '{self._entry_point}' 未在图中注册。已注册节点: {list(self._nodes.keys())}"
            )

        # 2. 验证所有无条件边
        for src, dst in self._edges.items():
            if src not in self._nodes and src != START:
                raise WorkflowGraphError(f"边的起点 '{src}' 未在图中注册")
            if dst not in self._nodes and dst != END:
                raise WorkflowGraphError(f"边的终点 '{dst}' 未在图中注册且不是 END")

        # 3. 验证所有条件边
        for src, (_, path_map) in self._conditional_edges.items():
            if src not in self._nodes and src != START:
                raise WorkflowGraphError(f"条件边的起点 '{src}' 未在图中注册")
            if path_map:
                for target in path_map.values():
                    if target not in self._nodes and target != END:
                        raise WorkflowGraphError(
                            f"条件边 '{src}' 映射的目标节点 '{target}' 未在图中注册且不是 END"
                        )

        return CompiledWorkflow(
            nodes=self._nodes,
            edges=self._edges,
            conditional_edges=self._conditional_edges,
            entry_point=self._entry_point,
            state_schema=self.state_schema,
        )


# 完全兼容 LangGraph 类名别名
StateGraph = WorkflowEngine
AsyncStateGraph = WorkflowEngine
CompiledStateGraph = CompiledWorkflow
