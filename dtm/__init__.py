"""Database Time Machine -- git-style history and time travel for SQLite."""

from .core import TimeMachine, TimeMachineError

__version__ = "0.1.0"
__all__ = ["TimeMachine", "TimeMachineError", "__version__"]
