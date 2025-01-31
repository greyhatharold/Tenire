"""
Shared enumerations and type definitions for the Tenire framework.

This module contains common enums, types, and constants used across different
components of the framework, ensuring consistency and reducing code duplication.
"""

from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Union, TypeVar, Coroutine, TYPE_CHECKING
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import asyncio

# Type definitions
T = TypeVar('T')

# Forward references for type hints
if TYPE_CHECKING:
    HandlerFunc = Union[Callable[['Signal'], None], Callable[['Signal'], Coroutine[Any, Any, None]]]
    FilterFunc = Callable[['Signal'], bool]
else:
    # Runtime-compatible type aliases
    HandlerFunc = Union[Callable[..., None], Callable[..., Coroutine[Any, Any, None]]]
    FilterFunc = Callable[[Any], bool]

@dataclass
class Signal:
    """Data structure for signals with type safety and metadata."""
    type: 'SignalType'
    data: Dict[str, Any]
    source: str = field(default="system")
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
    gui_context: Optional[Dict[str, Any]] = field(default=None)
    priority: int = field(default=0)  # Higher number = higher priority
    
    def __post_init__(self):
        """Validate signal data after initialization."""
        if not isinstance(self.type, SignalType):
            try:
                # Try to convert string or enum value to SignalType
                if isinstance(self.type, str):
                    self.type = SignalType[self.type]
                elif isinstance(self.type, int):
                    self.type = SignalType(self.type)
            except (KeyError, ValueError):
                raise ValueError(f"Signal type must be SignalType enum, got {type(self.type)}")
                
        if not isinstance(self.data, dict):
            raise ValueError(f"Signal data must be dict, got {type(self.data)}")
            
    def __lt__(self, other: 'Signal') -> bool:
        """Compare signals based on priority."""
        if not isinstance(other, Signal):
            return NotImplemented
        return self.priority < other.priority
        
    def __eq__(self, other: object) -> bool:
        """Compare signals for equality."""
        if not isinstance(other, Signal):
            return NotImplemented
        return (self.type == other.type and 
                self.data == other.data and
                self.source == other.source and
                self.timestamp == other.timestamp and
                self.priority == other.priority)

class SignalHandler:
    """Handler for signals with priority and filtering support."""
    
    def __init__(
        self,
        func: Callable[[Signal], Any],
        filter_func: Optional[Callable[[Signal], bool]] = None,
        priority: int = 0,
        is_gui: bool = False,
        is_cpu_bound: bool = False,
        is_async: Optional[bool] = None
    ):
        """Initialize signal handler."""
        self.func = func
        self.filter_func = filter_func
        self.priority = priority
        self.is_gui = is_gui
        self.is_cpu_bound = is_cpu_bound
        self.is_async = is_async if is_async is not None else asyncio.iscoroutinefunction(func)
        self.instance_ref = None
        
        # If func is a bound method, store weak reference to instance
        if hasattr(func, '__self__'):
            import weakref
            self.instance_ref = weakref.ref(func.__self__)
            
    async def __call__(self, signal: Signal) -> Any:
        """Execute the handler with filtering."""
        if self.filter_func and not self.filter_func(signal):
            return None
            
        if self.is_async:
            return await self.func(signal)
        else:
            return self.func(signal)
            
    def __eq__(self, other: 'SignalHandler') -> bool:
        """Compare handlers for equality."""
        return (
            isinstance(other, SignalHandler) and
            self.func == other.func and
            self.priority == other.priority
        )

class HealthStatus(Enum):
    """Enumeration of possible health states."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"

class HealthCheckLevel(Enum):
    """Enumeration of health check levels."""
    CRITICAL = "critical"  # Must be healthy for system to function
    WARNING = "warning"    # Issues may impact performance
    INFO = "info"         # Informational checks

class AutoResponseAction(Enum):
    """Enumeration of automated response actions."""
    RESTART_COMPONENT = "restart_component"
    CLEAR_QUEUE = "clear_queue"
    RESET_THREAD_POOL = "reset_thread_pool"
    RESOLVE_CYCLE = "resolve_cycle"
    CLEANUP_REFS = "cleanup_refs"
    REINITIALIZE = "reinitialize"
    LOG_ONLY = "log_only"

class SignalType(Enum):
    """Enumeration of supported signal types."""
    # Monitor signals
    MONITOR_STARTED = auto()  # Signal for monitor started
    MONITOR_STOPPED = auto()  # Signal for monitor stopped
    MONITOR_METRICS = auto()  # Signal for raw metrics data
    MONITOR_METRICS_UPDATED = auto()  # Signal for metrics update
    MONITOR_ALERT = auto()  # Signal for monitor alerts
    MONITOR_COMPONENT_REGISTERED = auto()  # Signal for component registration
    MONITOR_COMPONENT_UNREGISTERED = auto()  # Signal for component unregistration
    
    # Command signals
    COMMAND_RECEIVED = auto()  # Signal for received command
    COMMAND_PROCESSED = auto()  # Signal for processed command
    COMMAND_COMPLETED = auto()  # Signal for completed command
    COMMAND_CANCELLED = auto()  # Signal for cancelled command
    COMMAND_ERROR = auto()  # Signal for command error
    COMMAND_CONFIRMATION = auto()  # Signal for command confirmation status
    COMMAND_CONFIRMATION_REQUESTED = auto()  # Signal for command confirmation request
    COMMAND_CONFIRMATION_RESPONSE = auto()  # Signal for command confirmation response
    COMMAND_RESPONSE = auto()  # Signal for command response
    
    # Handler timing signals
    HANDLER_TIMING = auto()  # Signal for handler timing updates
    HANDLER_STARTED = auto()  # Signal for handler started
    HANDLER_COMPLETED = auto()  # Signal for handler completed
    HANDLER_ERROR = auto()  # Signal for handler errors
    
    # Browser-related signals
    BROWSER_STARTED = auto()  # Signal for browser session started
    BROWSER_CLOSED = auto()   # Signal for browser session closed
    BROWSER_COMMAND = auto()  # Signal for browser command execution
    BROWSER_COMMAND_COMPLETED = auto()  # Signal for browser command completion
    BROWSER_COMMAND_ERROR = auto()  # Signal for browser command error
    BROWSER_ACTION = auto()  # Signal for browser action execution
    BROWSER_ACTION_COMPLETED = auto()  # Signal for browser action completion
    BROWSER_ACTION_ERROR = auto()  # Signal for browser action error
    BROWSER_ERROR = auto()  # Signal for general browser errors
    BROWSER_STATUS = auto()  # Signal for browser status updates
    BROWSER_NAVIGATION = auto()  # Signal for browser navigation events
    BROWSER_INTERACTION = auto()  # Signal for browser interaction events
    BROWSER_STATE = auto()  # Signal for browser state updates
    BROWSER_HEALTH = auto()  # Signal for browser health status
    
    # Authentication signals  
    LOGIN_COMPLETED = auto()  # Signal for successful login
    LOGIN_FAILED = auto()     # Signal for failed login
    LOGIN_STATUS = auto()     # Signal type for login status
    SECURITY_CHALLENGE_COMPLETED = auto()  # Signal for completed security challenge
    SECURITY_CHALLENGE_DETECTED = auto()  # Added missing signal type
    
    # Betting signals
    BET_PLACED = auto()       # Signal type for bet placed
    BET_ERROR = auto()        # Signal for betting errors
    BALANCE_UPDATED = auto()  # Signal type for balance updated
    
    # GUI signals
    GUI_UPDATE = auto()           # Signal type for GUI updates
    GUI_ACTION = auto()           # Signal type for GUI-triggered actions 
    GUI_STATE = auto()            # Signal type for GUI state changes
    GUI_STATUS_UPDATED = auto()   # Signal for GUI status bar updates
    GUI_MESSAGE_RECEIVED = auto() # Signal for message received in GUI
    GUI_ERROR = auto()            # Signal for GUI errors
    
    # System signals
    ERROR = auto()                # Error signal
    CLEANUP_REQUESTED = auto()    # Signal for requesting cleanup/shutdown
    GUI_CLEANUP_REQUESTED = auto()  # Signal for GUI cleanup
    BROWSER_CLEANUP_REQUESTED = auto()  # Signal for browser cleanup
    AGENT_TASK_ERROR = auto()     # Signal for agent task errors
    AGENT_TASK_STARTED = auto()   # Signal for agent task started
    AGENT_TASK_COMPLETED = auto() # Signal for agent task completed
    AGENT_THOUGHT_GENERATED = auto() # Signal for agent thought generation
    
    # Safeguard signals
    SAFEGUARD_CHECK = auto()      # Signal for safeguard validation checks
    SAFEGUARD_VIOLATION = auto()  # Signal for safeguard violations
    SAFEGUARD_CONFIRMATION = auto() # Signal for safeguard confirmations
    SAFEGUARD_WARNING = auto()    # Signal for safeguard warnings
    SAFEGUARD_RESET = auto()      # Signal for safeguard reset events
    
    # Agent signals
    AGENT_MESSAGE = auto()  # For agent messages
    AGENT_RESPONSE = auto()  # For agent responses
    
    # RAG Pipeline signals
    RAG_DOCUMENT_ADDED = auto()  # Signal for document added to store
    RAG_DOCUMENT_REMOVED = auto()  # Signal for document removed from store
    RAG_EMBEDDING_COMPLETED = auto()  # Signal for embedding generation completed
    RAG_EMBEDDING_ERROR = auto()  # Signal for embedding generation error
    RAG_QUERY_STARTED = auto()  # Signal for query processing started
    RAG_QUERY_COMPLETED = auto()  # Signal for query processing completed
    RAG_QUERY_ERROR = auto()  # Signal for query processing error
    RAG_CACHE_UPDATED = auto()  # Signal for cache updates
    RAG_INDEX_UPDATED = auto()  # Signal for index updates
    RAG_RERANK_COMPLETED = auto()  # Signal for reranking completed
    RAG_BATCH_PROCESSED = auto()  # Signal for batch processing completed
    RAG_COMPONENT_INITIALIZED = auto()  # Signal for component initialization
    RAG_COMPONENT_ERROR = auto()  # Signal for component errors
    RAG_CLEANUP_STARTED = auto()  # Signal for cleanup started
    RAG_CLEANUP_COMPLETED = auto()  # Signal for cleanup completed
    
    # Health monitoring signals
    HEALTH_CHECK_STARTED = auto()  # Signal for health check started
    HEALTH_CHECK_COMPLETED = auto()  # Signal for health check completed
    HEALTH_CHECK_ERROR = auto()  # Signal for health check error
    COMPONENT_STATUS_CHANGED = auto()  # Signal for component status changes
    COMPONENT_DEGRADED = auto()  # Signal for component degradation
    COMPONENT_UNHEALTHY = auto()  # Signal for component unhealthy state
    COMPONENT_RECOVERED = auto()  # Signal for component recovery
    AUTO_RESPONSE_TRIGGERED = auto()  # Signal for automated response triggered
    AUTO_RESPONSE_COMPLETED = auto()  # Signal for automated response completed
    AUTO_RESPONSE_ERROR = auto()  # Signal for automated response error
    SYSTEM_HEALTH_REPORT = auto()  # Signal for system health report generation
    HEALTH_CHECK_RESULT = auto()  # Signal for health check result
    HEALTH_STATUS_UPDATED = auto()  # Signal for health status updates
    
    # Data Flow signals
    DATA_FLOW_STARTED = auto()  # Signal for data flow process started
    DATA_FLOW_STOPPED = auto()  # Signal for data flow process stopped
    DATA_FLOW_ERROR = auto()  # Signal for data flow errors
    DATA_FLOW_METRICS = auto()  # Signal for data flow metrics updates
    DATA_FLOW_ROUTE_CREATED = auto()  # Signal for new data route creation
    DATA_FLOW_ROUTE_DELETED = auto()  # Signal for data route deletion
    DATA_FLOW_STREAM_CREATED = auto()  # Signal for new data stream creation
    DATA_FLOW_STREAM_DELETED = auto()  # Signal for data stream deletion
    DATA_FLOW_ROUTE_STATUS = auto()  # Signal for route status updates
    DATA_FLOW_STREAM_STATUS = auto()  # Signal for stream status updates
    DATA_FLOW_BACKPRESSURE = auto()  # Signal for backpressure events
    DATA_FLOW_CLEANUP = auto()  # Signal for cleanup events
    DATA_FLOW_HEALTH_STATUS = auto()  # Signal for health status updates
    DATA_FLOW_RECOVERY = auto()  # Signal for recovery events
    DATA_FLOW_TRANSFORMATION = auto()  # Signal for data transformation events
    DATA_FLOW_VALIDATION = auto()  # Signal for data validation events
    DATA_FLOW_BATCH_PROCESSED = auto()  # Signal for batch processing events
    DATA_FLOW_QUEUE_STATUS = auto()  # Signal for queue status updates
    DATA_FLOW_BUFFER_STATUS = auto()  # Signal for buffer status updates
    DATA_FLOW_CLEANING_STAGE = auto()  # Signal for cleaning stage updates
    DATA_FLOW_PRIORITY_CHANGE = auto()  # Signal for priority changes
    DATA_FLOW_ROUTE_METRICS = auto()  # Signal for route metrics updates
    DATA_FLOW_STREAM_METRICS = auto()  # Signal for stream metrics updates
    DATA_FLOW_AGENT_STATUS = auto()  # Signal for agent status updates
    DATA_FLOW_BOTTLENECK = auto()  # Signal for data flow bottleneck detection
    DATA_FLOW_RECOVERED = auto()  # Signal for data flow recovery
    DATA_FLOW_THRESHOLD_EXCEEDED = auto()  # Signal for threshold exceeded
    DATA_FLOW_OPTIMIZATION_NEEDED = auto()  # Signal for optimization needed
    
    # Resource management signals
    RESOURCE_LIMIT_REACHED = auto()  # Signal for resource limit reached
    RESOURCE_RELEASED = auto()  # Signal for resource released
    RESOURCE_DEADLOCK_DETECTED = auto()  # Signal for deadlock detection
    RESOURCE_STARVATION_DETECTED = auto()  # Signal for resource starvation
    
    # Agent-specific signals
    AGENT_RESOURCE_WARNING = auto()  # Signal for agent resource warning
    AGENT_TASK_CANCELLED = auto()  # Signal for agent task cancellation
    AGENT_CACHE_CLEARED = auto()  # Signal for agent cache cleared
    AGENT_EMERGENCY_CLEANUP = auto()  # Signal for emergency cleanup
    AGENT_RESOURCE_RECOVERED = auto()  # Signal for resource recovery
    AGENT_DEPENDENCY_ERROR = auto()  # Signal for dependency error
    AGENT_COMPONENT_DEGRADED = auto()  # Signal for component degradation
    AGENT_COMPONENT_RECOVERED = auto()  # Signal for component recovery
    
    # Component integration signals
    COMPONENT_DEPENDENCY_CYCLE = auto()  # Signal for dependency cycle detection
    COMPONENT_INITIALIZATION_ERROR = auto()  # Signal for initialization error
    COMPONENT_CLEANUP_ERROR = auto()  # Signal for cleanup error
    COMPONENT_STATE_CHANGED = auto()  # Signal for state change

    def __init__(self, *args):
        """Initialize enum member with validation.
        
        Args:
            *args: Variable length argument list passed by Enum machinery
        """
        # Call parent init first
        super().__init__()
        # Validate after initialization
        self._validate_value()
        
    def _validate_value(self) -> None:
        """Validate the enum value is unique."""
        cls = self.__class__
        duplicates = [other for other in cls if other is not self and other.value == self.value]
        if duplicates:
            raise ValueError(f'Duplicate value {self.value} found for {self.name} and {duplicates[0].name}')
            
    @classmethod
    def get_by_category(cls, category: str) -> List['SignalType']:
        """Get all signal types for a given category.
        
        Args:
            category: The category to filter by (e.g. 'COMMAND', 'GUI', etc.)
            
        Returns:
            List of SignalType enums in that category
        """
        return [signal for signal in cls if signal.name.startswith(category)]
        
    @classmethod
    def validate_no_duplicates(cls) -> None:
        """Validate that no duplicate values exist in the enum.
        
        Raises:
            ValueError: If duplicate values are found
        """
        values = {}
        for signal in cls:
            if signal.value in values:
                raise ValueError(
                    f'Duplicate value {signal.value} found for {signal.name} '
                    f'and {values[signal.value].name}'
                )
            values[signal.value] = signal

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

@dataclass
class MonitorAlert:
    """Alert from the monitoring system."""
    level: str  # 'error' or 'warning'
    message: str
    component: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    details: Dict[str, Any] = field(default_factory=dict)

@dataclass
class MonitorComponent:
    """Registered component in the monitoring system."""
    name: str
    registration_time: datetime = field(default_factory=datetime.now)
    last_metric_update: Optional[datetime] = None
    metrics_history: List[Dict[str, Any]] = field(default_factory=list)
    alerts: List[MonitorAlert] = field(default_factory=list)
    history_limit: int = 100

class DataFlowPriority(Enum):
    """Priority levels for data flow processing."""
    CRITICAL = 3  # Highest priority, process immediately
    HIGH = 2      # High priority, process soon
    NORMAL = 1    # Normal priority
    LOW = 0       # Low priority, process when resources available
    
class DataFlowStatus(Enum):
    """Status of data flow components."""
    ACTIVE = "active"           # Component is active and processing
    INACTIVE = "inactive"       # Component is inactive
    PAUSED = "paused"          # Component is temporarily paused
    ERROR = "error"            # Component is in error state
    BACKPRESSURE = "backpressure"  # Component is applying backpressure
    CLEANING = "cleaning"      # Component is performing cleanup
    RECOVERING = "recovering"  # Component is recovering from error
    
class DataCleaningStage(Enum):
    """Stages in the data cleaning pipeline."""
    VALIDATION = "validation"    # Data validation stage
    CLEANING = "cleaning"        # Data cleaning stage
    TRANSFORMATION = "transformation"  # Data transformation stage
    COMPRESSION = "compression"  # Data compression stage
    ENCODING = "encoding"        # Data encoding stage
    
@dataclass
class DataFlowMetrics:
    """Metrics for data flow components."""
    processed_count: int = 0
    error_count: int = 0
    latency: float = 0.0
    queue_size: int = 0
    buffer_usage: float = 0.0
    last_processed: Optional[datetime] = None
    cleaning_stats: Dict[str, int] = field(default_factory=dict)
    transformation_stats: Dict[str, int] = field(default_factory=dict)
    
@dataclass
class DataFlowHealth:
    """Health status for data flow components."""
    status: DataFlowStatus
    metrics: DataFlowMetrics
    last_error: Optional[str] = None
    last_check: datetime = field(default_factory=datetime.now)
    details: Dict[str, Any] = field(default_factory=dict)

@dataclass
class TimingConfig:
    """Configuration for component timing."""
    min_interval: float  # Minimum interval between operations
    max_interval: float  # Maximum interval for operations
    burst_limit: int    # Maximum operations in burst
    cooldown: float     # Cooldown period after burst
    priority: int       # Priority level (higher = more important)