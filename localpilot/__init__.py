"""LocalPilot -- a local, open-model Windows desktop agent.

Perceives the active window via UI Automation, asks a local LLM (served by
Ollama) for one action at a time, and executes it via UIA patterns.
"""
from .config import Config, DEFAULT
from .agent import run

__all__ = ["Config", "DEFAULT", "run"]
__version__ = "0.9.3"
