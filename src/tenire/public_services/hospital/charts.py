"""
Core data charts for the health monitoring system.

This module contains the data structures and types used across the health
monitoring system, following a clean domain model approach for charting and
tracking health states.
"""

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

class HealthStatus(enum.Enum):
    """Enumeration of possible health states."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"

class HealthCheckLevel(enum.Enum):
    """Enumeration of health check levels."""
    CRITICAL = "critical"  # Must be healthy for system to function
    WARNING = "warning"    # Issues may impact performance
    INFO = "info"         # Informational checks

class AutoResponseAction(enum.Enum):
    """Enumeration of automated response actions."""
    RESTART_COMPONENT = "restart_component"
    CLEAR_QUEUE = "clear_queue"
    RESET_THREAD_POOL = "reset_thread_pool"
    RESOLVE_CYCLE = "resolve_cycle"
    CLEANUP_REFS = "cleanup_refs"
    REINITIALIZE = "reinitialize"
    LOG_ONLY = "log_only"

@dataclass
class HealthCheckResult:
    """Result of a health check."""
    component: str
    status: HealthStatus
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    level: HealthCheckLevel = field(default=HealthCheckLevel.INFO)
    latency: float = field(default=0.0)
    error: Optional[Exception] = None

@dataclass
class HealthCheck:
    """Definition of a health check."""
    name: str
    check_fn: Callable[[], Any]
    interval: timedelta
    level: HealthCheckLevel
    timeout: float = 5.0
    last_run: Optional[datetime] = None
    last_result: Optional[HealthCheckResult] = None
    is_async: bool = True

@dataclass
class AutoResponse:
    """Definition of an automated response to a health check."""
    action: AutoResponseAction
    conditions: List[Callable[[HealthCheckResult], bool]]
    max_attempts: int = 3
    cooldown: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    last_attempt: Optional[datetime] = None
    attempt_count: int = 0
    is_async: bool = True 