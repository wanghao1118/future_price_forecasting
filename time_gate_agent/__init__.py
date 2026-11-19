"""Time gate agent: independently reviewed news before a verified result date."""

AGENT_NAME = "time gate agent"
__version__ = "1.0.0"

from .agent import TimeGateAgent
from .guard import FetchedPage

__all__ = ["AGENT_NAME", "TimeGateAgent", "FetchedPage"]
