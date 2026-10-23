"""
AgenticSRE Tool Base Framework
Provides SnailTool ABC, ToolResult dataclass, and singleton ToolRegistry.
"""

import time
import logging
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable

logger = logging.getLogger(__name__)


# ───────────── Tool Result ─────────────

@dataclass
class ToolResult:
    """Universal return envelope for all tool executions."""
    success: bool
    data: Any = None
    error: str = ""
    duration_ms: float = 0.0
    source: str = ""        # which tool produced this
    truncated: bool = False  # whether output was truncated for LLM context

    def to_context(self, max_chars: int = 6000) -> str:
        """Format result for LLM context window, with optional truncation."""
        if self.success:
            text = str(self.data) if self.data is not None else "(no data)"
        else:
            text = f"ERROR: {self.error}"
        
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated, {len(text)} total chars]"
            self.truncated = True
        
        return f"[{self.source}] {text}"


# ───────────── Tool Base Class ─────────────

class SRETool(ABC):
    """Abstract base class for all AgenticSRE tools."""

    name: str = "unnamed_tool"
    description: str = "No description"
    
    @abstractmethod
    def _execute(self, **kwargs) -> ToolResult:
        """Subclass must implement actual tool logic."""
        ...

    def execute(self, **kwargs) -> ToolResult:
        """Safe wrapper with timing, error handling, and logging."""
        start = time.time()
        try:
            result = self._execute(**kwargs)
            result.duration_ms = (time.time() - start) * 1000
            result.source = self.name
            logger.debug(f"Tool [{self.name}] OK in {result.duration_ms:.0f}ms")
            return result
        except Exception as e:
            duration = (time.time() - start) * 1000
            logger.error(f"Tool [{self.name}] FAILED: {e}\n{traceback.format_exc()}")
            return ToolResult(
                success=False,
                error=str(e),
                duration_ms=duration,
                source=self.name,
            )

    def get_schema(self) -> Dict[str, Any]:
        """Return OpenAI function-calling compatible JSON schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._parameters_schema(),
            }
        }

    def _parameters_schema(self) -> Dict[str, Any]:
        """Override to provide parameter schema. Default: no params."""
        return {"type": "object", "properties": {}, "required": []}

    def health_check(self) -> bool:
        """Override for connectivity probe. Default: True."""
        return True


# ───────────── Tool Registry ─────────────

class ToolRegistry:
    """Singleton registry for all SRETool instances."""

    _instance: Optional['ToolRegistry'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools: Dict[str, SRETool] = {}
            cls._instance._categories: Dict[str, List[str]] = {}
        return cls._instance

    def register(self, tool: SRETool, category: str = "general") -> None:
        """Register a tool by its name."""
        self._tools[tool.name] = tool
        if category not in self._categories:
            self._categories[category] = []
        if tool.name not in self._categories[category]:
            self._categories[category].append(tool.name)
        logger.debug(f"Registered tool: {tool.name} [{category}]")

    def get(self, name: str) -> Optional[SRETool]:
        """Retrieve a tool by name."""
        return self._tools.get(name)

    def execute(self, name: str, **kwargs) -> ToolResult:
        """Execute a tool by name."""
        tool = self.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"Tool '{name}' not found", source="registry")
        return tool.execute(**kwargs)

    def list_tools(self, category: Optional[str] = None) -> List[Dict[str, str]]:
        """List all registered tools, optionally filtered by category."""
        if category:
            names = self._categories.get(category, [])
            tools = [self._tools[n] for n in names if n in self._tools]
        else:
            tools = list(self._tools.values())
        return [{"name": t.name, "description": t.description} for t in tools]

    def get_schemas(self, category: Optional[str] = None) -> List[Dict]:
        """Get OpenAI function schemas for all tools."""
        if category:
            names = self._categories.get(category, [])
            tools = [self._tools[n] for n in names if n in self._tools]
        else:
            tools = list(self._tools.values())
        return [t.get_schema() for t in tools]

    def categories(self) -> List[str]:
        """List all tool categories."""
        return list(self._categories.keys())

    def health_check_all(self) -> Dict[str, bool]:
        """Run health checks on all tools."""
        return {name: tool.health_check() for name, tool in self._tools.items()}

    def reset(self) -> None:
        """Clear all registered tools (for testing)."""
        self._tools.clear()
        self._categories.clear()

    @classmethod
    def get_instance(cls) -> 'ToolRegistry':
        """Get the singleton instance."""
        return cls()

    @classmethod
    def isolated(cls) -> 'ToolRegistry':
        """Create a request-scoped registry that does not mutate the singleton."""
        instance = object.__new__(cls)
        instance._tools = {}
        instance._categories = {}
        return instance

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
