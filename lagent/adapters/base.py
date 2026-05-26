"""Base classes for external agent adapters.

Provides ``BaseExternalAgent`` and ``AsyncExternalAgent`` — abstract base
classes that wrap external agent frameworks (CLI tools or Python SDKs)
into lagent's Agent protocol.

These adapters are fully compatible with lagent's ecosystem: they can be
used as ``agent_engine`` in InterclawApp, placed into Sequential chains,
managed by AgentService, and discovered by AgentLoader.

Key design:
    - ``forward()`` = execute external agent, return final output as AgentMessage
    - ``state_dict()`` = memory + LLM trace from Proxy (if enabled)
    - ``llm=None`` because external frameworks bring their own reasoning engine
    - ``setup()`` is lazy (called on first forward, not at init)
    - Proxy integration via ``_build_env()`` injects base_url + session key

Usage::

    class MyCLIAgent(AsyncExternalAgent):
        async def setup(self):
            ...  # verify binary exists

        async def run_external_async(self, task, **kwargs):
            ...  # subprocess call, return stdout

    agent = MyCLIAgent(name="my-agent", timeout=300)
    result = await agent("Fix the bug in main.py")
    trace = agent.state_dict().get('llm_trace', [])
"""

import json
import os
from abc import abstractmethod
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

from lagent.agents.agent import Agent, AsyncAgentMixin
from lagent.schema import AgentMessage


def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable copy for daemon responses."""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class BaseExternalAgent(Agent):
    """Abstract base for wrapping external agent frameworks as lagent Agents.

    Subclasses implement ``setup()`` and ``run_external()``.
    The ``forward()`` method handles the lifecycle:
    setup → build env → run → wrap output as AgentMessage.

    This class does NOT require an LLM, memory, or aggregator from lagent.
    The external framework provides its own reasoning engine.

    Args:
        name: Agent name, used as AgentMessage.sender.
        description: Human-readable description.
        working_dir: Working directory for the external agent.
        env_vars: Extra environment variables for the external agent.
        timeout: Maximum execution time in seconds. None = no limit.
        proxy: Optional LLMProxyRecorder for trajectory capture.
        hooks: Optional hooks (same as Agent).
    """

    def __init__(
        self,
        name: Optional[str] = None,
        description: Optional[str] = None,
        working_dir: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        proxy: Any = None,
        **kwargs,
    ):
        # Don't pass llm, template, output_format, aggregator — not needed
        super().__init__(
            llm=None,
            name=name,
            description=description,
            hooks=kwargs.pop('hooks', None),
        )
        self.working_dir = working_dir
        self.env_vars = env_vars or {}
        self.timeout = timeout
        self.session_id = uuid4().hex[:8]
        self.proxy = proxy
        self._setup_done = False

    @abstractmethod
    def setup(self) -> None:
        """One-time initialization (verify binary / check SDK import).

        Called lazily on first ``forward()`` call. Must be idempotent.
        """

    @abstractmethod
    def run_external(self, task: str, **kwargs) -> str:
        """Execute the external agent synchronously.

        Args:
            task: The task/prompt string.

        Returns:
            The external agent's textual output.

        Raises:
            RuntimeError: If the external agent fails.
            TimeoutError: If execution exceeds self.timeout.
        """

    def _build_env(self) -> dict:
        """Build environment variables dict with proxy injection."""
        env = os.environ.copy()
        env.update(self.env_vars)
        if self.proxy:
            session_key = f"sk-proxy-{self.session_id}"
            env.update({
                'OPENAI_BASE_URL': self.proxy.url,
                'OPENAI_API_KEY': session_key,
                'ANTHROPIC_BASE_URL': self.proxy.url,
                'ANTHROPIC_API_KEY': session_key,
            })
        return env

    def _extract_task(self, messages: tuple) -> str:
        """Join multiple AgentMessage contents into a single task string."""
        parts = []
        for m in messages:
            if isinstance(m, AgentMessage):
                parts.append(str(m.content))
            else:
                parts.append(str(m))
        return '\n'.join(parts)

    def forward(self, *message: AgentMessage, **kwargs) -> Union[AgentMessage, str]:
        """Lagent Agent protocol implementation.

        Extracts task from messages, runs external agent, wraps result.
        """
        task = self._extract_task(message)
        if not self._setup_done:
            self.setup()
            self._setup_done = True

        try:
            output = self.run_external(task, **kwargs)
        except Exception as exc:
            return AgentMessage(
                sender=self.name,
                content=f"External agent failed: {exc}",
                extra_info={'error': str(exc), 'adapter': self.__class__.__name__},
            )

        return AgentMessage(
            sender=self.name,
            content=output,
            extra_info={'adapter': self.__class__.__name__, 'session_id': self.session_id},
        )

    def state_dict(self, prefix='', destination=None) -> Dict:
        dest = super().state_dict(prefix=prefix, destination=destination)
        if self.proxy:
            dest[prefix + 'llm_trace'] = self.proxy.get_records(self.session_id)
        return dest

    def get_messages(self, prefix='', destination=None) -> Dict[str, List[dict]]:
        """Return messages for black-box external agents.

        If a proxy recorder is attached, prefer the proxy-captured model
        conversation because it reflects the real request/response sequence
        used by the external agent. Otherwise fall back to lagent's wrapper
        memory, which contains the user task and final external-agent output.

        Top-level external agents also expose ``policy_agent.*`` aliases so
        sandbox RL code can consume them through the same keys used by the
        white-box ``FunctionCallAgent`` harness.
        """
        if destination is None:
            destination = {}

        messages = self._get_proxy_messages()
        tools: List[dict] = []
        if messages is None:
            local = super().get_messages(prefix=prefix, destination={})
            messages = local.get(prefix + 'messages', [])
            tools = local.get(prefix + 'tools', [])

        messages = _json_safe(messages)
        tools = _json_safe(tools)
        destination[prefix + 'messages'] = messages
        destination[prefix + 'tools'] = tools

        if not prefix:
            destination['policy_agent.messages'] = messages
            destination['policy_agent.tools'] = tools
        return destination

    def _get_proxy_messages(self) -> Optional[List[dict]]:
        if self.proxy is None or not hasattr(self.proxy, 'get_messages'):
            return None
        try:
            traces = self.proxy.get_messages()
        except Exception:
            return None
        if not traces:
            return None
        if isinstance(traces, list) and all(isinstance(item, dict) for item in traces):
            return traces
        if not isinstance(traces, list):
            return None
        candidates = [trace for trace in traces if isinstance(trace, list)]
        if not candidates:
            return None
        return max(candidates, key=len)

    def load_state_dict(self, state_dict: Dict):
        # Filter out llm_trace keys before passing to parent
        filtered = {
            k: v for k, v in state_dict.items()
            if not k.endswith('llm_trace')
        }
        # Parent expects exact key match, add missing memory key if needed
        if not any(k.endswith('memory') for k in filtered):
            filtered['' + 'memory'] = []
        super().load_state_dict(filtered)


class AsyncExternalAgent(AsyncAgentMixin, BaseExternalAgent):
    """Async variant of BaseExternalAgent.

    Subclasses implement ``run_external_async()`` instead of
    ``run_external()``.
    """

    @abstractmethod
    async def run_external_async(self, task: str, **kwargs) -> str:
        """Async version of run_external."""

    def run_external(self, task: str, **kwargs) -> str:
        """Sync fallback — not used in async path."""
        raise NotImplementedError(
            "Use run_external_async() for AsyncExternalAgent"
        )

    async def forward(self, *message: AgentMessage, **kwargs) -> Union[AgentMessage, str]:
        task = self._extract_task(message)
        if not self._setup_done:
            self.setup()
            self._setup_done = True

        # Lazily start proxy if present
        if self.proxy and not self.proxy.is_running:
            await self.proxy.start()

        try:
            output = await self.run_external_async(task, **kwargs)
        except Exception as exc:
            return AgentMessage(
                sender=self.name,
                content=f"External agent failed: {exc}",
                extra_info={'error': str(exc), 'adapter': self.__class__.__name__},
            )

        return AgentMessage(
            sender=self.name,
            content=output,
            extra_info={'adapter': self.__class__.__name__, 'session_id': self.session_id},
        )
