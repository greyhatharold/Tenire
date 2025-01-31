"""
Agent management module for the Tenire framework.

This module handles the configuration and management of agents,
providing a clean interface for automated operations through various toolkits.
"""
# Standard library imports
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, TYPE_CHECKING
from datetime import datetime
import multiprocessing
import psutil
import threading

# Third-party imports
from browser_use import Agent
from langchain.callbacks.base import BaseCallbackHandler
from langchain.schema import BaseMessage
from langchain_openai import ChatOpenAI
import qasync

# Local imports
from tenire.actions.risky.betting_actions import ACTION_DESCRIPTIONS, betting_controller
from tenire.core.config import config_manager
from tenire.core.codex import Signal, SignalType
from tenire.core.immutables import AGENT_CONFIG, INITIAL_AGENT_STATE
from tenire.organizers.scheduler import orchestrator, component_scheduler, TimingConfig, InitializationTask
from tenire.organizers.concurrency.concurrency_manager import concurrency_manager
from tenire.data.cacher import ModelCache, TieredCache, CachePolicy, cache_decorator
from tenire.utils.logger import get_logger
from tenire.organizers.compactor import compactor
from tenire.servicers import SignalManager
from tenire.core.event_loop import event_loop_manager
from tenire.core.container import container
from tenire.structure.component_registry import component_registry
from tenire.structure.component_types import ComponentType
from tenire.structure.dependency_graph import DependencyError
from tenire.servicers.agent_data_pipeline import AgentDataPipeline, AgentDataConfig, DataFlowPriority, DataCleaningStage
from tenire.toolkit.manager import toolkit_manager
from tenire.toolkit.betting import BettingToolkit

if TYPE_CHECKING:
    from tenire.gui.gui import SidebarGUI

# Configure logging
logger = get_logger(__name__)

# Initialize caches
agent_model_cache = ModelCache(
    memory_size=5,  # Keep few models in memory
    disk_size=10_000_000_000,  # 10GB for model storage
    disk_path=Path("data/cache/agent_models"),
    model_configs={
        'chatgpt': {
            'max_size': 1_000_000,  # 1MB for config
            'ttl': 3600,  # 1 hour
            'compression': False
        }
    }
)

agent_cache = TieredCache(
    memory_size=1000,
    disk_size=1_000_000,
    disk_path=Path("data/cache/agent_ops"),
    policy=CachePolicy.LRU,
    ttl=3600  # 1 hour cache for agent operations
)

class BatchRequestHandler(BaseCallbackHandler):
    """Handler for batched ChatGPT requests."""
    
    def __init__(self):
        """Initialize the batch request handler with safe defaults."""
        self.pending_requests: List[BaseMessage] = []
        
        # Default values from AGENT_CONFIG
        batch_config = AGENT_CONFIG['batch_request']
        self.batch_size = batch_config['batch_size']
        self.request_timeout = batch_config['request_timeout']
        self.max_retries = batch_config['max_retries']
        self.retry_delay = batch_config['retry_delay']
        
        try:
            # Try to get config from config manager
            betting_config = config_manager.get_betting_config()
            
            # Safely get values with fallbacks to current defaults
            if hasattr(betting_config, 'batch_size'):
                self.batch_size = betting_config.batch_size
            if hasattr(betting_config, 'request_timeout'):
                self.request_timeout = betting_config.request_timeout
            if hasattr(betting_config, 'max_retries'):
                self.max_retries = betting_config.max_retries
            if hasattr(betting_config, 'retry_delay'):
                self.retry_delay = betting_config.retry_delay
                
            logger.debug(f"Using batch config - size: {self.batch_size}, timeout: {self.request_timeout}")
        except Exception as e:
            logger.warning(f"Using default batch request values: {str(e)}")

    async def _get_concurrency_manager(self):
        """Lazy import and get concurrency manager to avoid circular dependencies."""
        return container.get('concurrency_manager')

    async def _get_signal_manager(self):
        """Lazy import and get signal manager to avoid circular dependencies."""
        return container.get('signal_manager')
            
    async def on_llm_start(self, *args, **kwargs):
        """Handle the start of an LLM request."""
        if len(self.pending_requests) >= self.batch_size:
            await self._process_batch()
            
    async def _process_batch(self):
        """Process a batch of requests."""
        if not self.pending_requests:
            return
            
        batch = self.pending_requests[:self.batch_size]
        self.pending_requests = self.pending_requests[self.batch_size:]
        
        concurrency_manager = await self._get_concurrency_manager()
        if not concurrency_manager:
            logger.error("Concurrency manager not available")
            return
        
        async with concurrency_manager.semaphores.acquire("llm_batch", self.batch_size):
            for attempt in range(self.max_retries):
                try:
                    # Process batch with timeout
                    async with asyncio.timeout(self.request_timeout):
                        tasks = [
                            concurrency_manager.async_tasks.create_task(
                                self._process_request(req),
                                f"llm_request_{i}"
                            )
                            for i, req in enumerate(batch)
                        ]
                        await asyncio.gather(*tasks)
                    break
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        logger.error(f"Failed to process batch after {self.max_retries} attempts: {str(e)}")
                        raise
                    await asyncio.sleep(self.retry_delay)
                
    async def _process_request(self, request: BaseMessage):
        """Process a single request from the batch."""
        try:
            # Actual request processing will be handled by LangChain
            pass
        except Exception as e:
            logger.error(f"Failed to process request: {str(e)}")
            raise

    def _get_llm_config(self) -> Dict[str, Any]:
        """
        Get cached LLM configuration.
        
        Returns:
            Dict containing LLM configuration parameters
        """
        # Base configuration that's always supported
        base_config = {
            "api_key": config_manager.OPENAI_API_KEY.get_secret_value(),
            "model": config_manager.CHATGPT_MODEL,
            "temperature": config_manager.CHATGPT_TEMPERATURE,
            "max_tokens": config_manager.CHATGPT_MAX_TOKENS,
            "top_p": config_manager.CHATGPT_TOP_P,
            "frequency_penalty": config_manager.CHATGPT_FREQUENCY_PENALTY,
            "presence_penalty": config_manager.CHATGPT_PRESENCE_PENALTY,
            "cache": True,  # Enable LLM response caching
            "callbacks": [self],  # Add batch request handler
            "streaming": False,  # Disable streaming for batch processing
            "n": 1,  # Single completion per request for consistent batching
        }

        try:
            # Get model-specific kwargs
            model_kwargs = config_manager.get_model_kwargs()
            if model_kwargs:
                base_config.update(model_kwargs)  # Directly update base config
        except Exception as e:
            logger.warning(f"Failed to get model kwargs: {str(e)}")

        return base_config

class AsyncSignalEmitter:
    """Helper class to safely emit signals in Qt context with improved thread safety."""
    
    def __init__(self) -> None:
        """Initialize the signal emitter."""
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_lock: asyncio.Lock = asyncio.Lock()
        
    async def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """
        Ensure we have an event loop with proper thread safety.
        
        Returns:
            asyncio.AbstractEventLoop: The event loop instance
            
        Raises:
            RuntimeError: If unable to obtain or create a valid event loop
        """
        async with self._loop_lock:
            if self._loop is None:
                try:
                    # First try to get Qt event loop
                    self._loop = event_loop_manager.get_event_loop(for_qt=True)
                except RuntimeError:
                    try:
                        # Try getting current loop
                        self._loop = asyncio.get_event_loop()
                    except RuntimeError:
                        # Create Qt loop as last resort
                        try:
                            self._loop = qasync.QEventLoop()
                            asyncio.set_event_loop(self._loop)
                        except Exception as e:
                            raise RuntimeError(f"Failed to create event loop: {str(e)}")
                            
            if not self._loop:
                raise RuntimeError("Could not obtain or create event loop")
                
            return self._loop
            
    async def emit_signal(self, signal: Signal) -> None:
        """
        Safely emit a signal with proper error handling.
        
        Args:
            signal: The signal to emit
            
        Raises:
            RuntimeError: If signal emission fails
        """
        try:
            loop = await self._ensure_loop()
            if not loop.is_running():
                logger.warning("Event loop not running during signal emission")
                return
            
            # Get current loop for comparison
            try:
                current_loop = asyncio.get_event_loop()
            except RuntimeError:
                current_loop = None
            
            # Ensure signal emission happens in the correct event loop
            if current_loop is not loop:
                future = asyncio.run_coroutine_threadsafe(
                    SignalManager().emit(signal), 
                    loop
                )
                await asyncio.wrap_future(future)
            else:
                await SignalManager().emit(signal)
                
        except Exception as e:
            error_msg = f"Error emitting signal: {str(e)}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

class GUIProcess(multiprocessing.Process):
    """Process for running the GUI application."""
    
    def __init__(
        self,
        command_queue: multiprocessing.Queue,
        response_queue: multiprocessing.Queue,
        width: int = 400
    ) -> None:
        """
        Initialize the GUI process.
        
        Args:
            command_queue: Queue for receiving commands
            response_queue: Queue for sending responses
            width: Width of the GUI window
        """
        super().__init__()
        self.command_queue = command_queue
        self.response_queue = response_queue
        self.width = width
        self._state: Dict[str, Any] = {
            'status': 'initialized',
            'is_ready': False,
            'error': None,
            'last_command': None,
            'last_response': None,
            'cleanup_tasks': set(),
            'initialization_attempts': 0
        }
        self.daemon = True  # Process will be terminated when main process exits
        self._signal_emitter = AsyncSignalEmitter()
        self._initialized = False
        self._shutdown_event = asyncio.Event()
        self._startup_event = asyncio.Event()
        self._cleanup_handlers: List[Callable[[], None]] = []
        
        # Register with component registry
        self._register_with_registry()
        
    def _register_with_registry(self) -> None:
        """Register the GUI process with the component registry."""
        try:
            # Define timing config for the GUI process
            timing_config = TimingConfig(
                min_interval=0.1,
                max_interval=1.0,
                burst_limit=25,
                cooldown=0.5,
                priority=85
            )
            
            # Register with component registry
            component_registry.register_component(
                name="gui_process",
                component=self,
                component_type=ComponentType.GUI,
                provides=["gui_interface", "command_processing"],
                dependencies={"signal_manager", "event_loop_manager"},
                priority=60,
                health_checks={
                    "process_health": self._check_process_health,
                    "queue_health": self._check_queue_health
                },
                timing_config=timing_config,
                cleanup_priority=90,
                tags={"gui", "process", "frontend"},
                is_critical=False
            )
            
            logger.debug("Registered GUI process with component registry")
            
        except Exception as e:
            logger.error(f"Error registering with component registry: {str(e)}")
            
    async def _check_process_health(self) -> tuple[bool, str]:
        """Check the health of the GUI process."""
        try:
            if not self.is_alive():
                return False, "GUI process is not running"
            if self._state.get('error'):
                return False, f"GUI process error: {self._state['error']}"
            return True, "GUI process is healthy"
        except Exception as e:
            return False, f"Error checking process health: {str(e)}"
            
    async def _check_queue_health(self) -> tuple[bool, str]:
        """Check the health of command and response queues."""
        try:
            if self.command_queue.full():
                return False, "Command queue is full"
            if self.response_queue.full():
                return False, "Response queue is full"
            return True, "Queues are healthy"
        except Exception as e:
            return False, f"Error checking queue health: {str(e)}"

class AgentManager:
    """
    Manages agents for automated operations.
    
    This class handles agent configuration, initialization, and execution,
    providing a clean interface for automated operations through various toolkits.
    """
    _instance = None
    _lock = asyncio.Lock()
    _active_tasks = set()
    _max_task_retries = AGENT_CONFIG['max_task_retries'] * 2
    _task_timeout = AGENT_CONFIG['task_timeout'] * 1.5
    _retry_delay = AGENT_CONFIG['retry_delay'] * 1.5
    _timing_config = TimingConfig(
        min_interval=0.1,
        max_interval=1.0,
        burst_limit=25,
        cooldown=0.5,
        priority=85
    )
    _required_dependencies = {
        'signal_manager',
        'concurrency_manager',
        'browser_integration',
        'compactor',
        'data_manager',
        'component_doctor',
        'data_seive',  # Add data seive dependency
        'data_flow_agent'  # Add data flow agent dependency
    }
    
    # Add resource monitoring thresholds
    _resource_thresholds = {
        'memory_percent': 85.0,  # Warn if memory usage exceeds 85%
        'cpu_percent': 75.0,     # Warn if CPU usage exceeds 75%
        'task_queue_size': 50,   # Warn if task queue exceeds 50 items
        'active_threads': 20,    # Warn if active threads exceed 20
        'pending_callbacks': 100  # Warn if pending callbacks exceed 100
    }

    def __new__(cls, browser_integration):
        if not cls._instance:
            cls._instance = super(AgentManager, cls).__new__(cls)
        return cls._instance

    def __init__(self, browser_integration):
        """Initialize the agent manager."""
        if not hasattr(self, '_initialized'):
            self._initialized = True
            self._browser_integration = browser_integration
            self._agent = None
            self._batch_handler = BatchRequestHandler()
            self._setup_logs_directory()
            self._task_semaphore = asyncio.Semaphore(1)
            self._shutdown_event = asyncio.Event()
            self._task_queue = asyncio.Queue()
            
            # Initialize state from immutables
            state = INITIAL_AGENT_STATE.copy()
            self._current_task = state['current_task']
            self._active_tasks = state['active_tasks']
            self._task_attempts = state['task_attempts']
            self._task_results = state['task_results']
            
            # Initialize command handlers
            self._command_handlers = {
                'place_bet': self._handle_place_bet_command,
                'check_balance': self._handle_check_balance_command,
                'analyze_history': self.analyze_betting_history,
                'optimize_strategy': self.optimize_betting_strategy,
                'browser_action': self._handle_browser_action_command
            }
            
            # Initialize data pipeline
            self._data_pipeline = None
            self._pipeline_config = AgentDataConfig(
                batch_size=AGENT_CONFIG['batch_size'],
                max_queue_size=AGENT_CONFIG['max_queue_size'],
                cleaning_required=True,
                priority=DataFlowPriority.HIGH,
                cleaning_stage=DataCleaningStage.VALIDATION,
                cache_size=AGENT_CONFIG['cache_size'],
                prefetch_size=5
            )
            
            # Start caches
            asyncio.create_task(self._initialize_caches())
            
            # Register with component registry
            asyncio.create_task(self._register_with_registry())
            
            # Register with orchestrator and scheduler
            asyncio.create_task(self._register_with_orchestrator())
            asyncio.create_task(self._register_signal_handlers())
            
            # Start task processor
            asyncio.create_task(self._process_task_queue())

    async def _initialize_caches(self):
        """Initialize caching system."""
        try:
            await agent_model_cache.start()
            await agent_cache.start()
            logger.info("Agent caching system initialized")
        except Exception as e:
            logger.error(f"Failed to initialize caches: {str(e)}")

    async def _register_with_registry(self) -> None:
        """Register the agent manager with enhanced dependency tracking."""
        try:
            # Verify all required dependencies are available
            missing_deps = []
            for dep in self._required_dependencies:
                if not container.get(dep):
                    missing_deps.append(dep)
            
            if missing_deps:
                raise DependencyError(f"Missing required dependencies: {', '.join(missing_deps)}")

            # Register with component registry
            await component_registry.register_component(
                name="agent_manager",
                component=self,
                component_type=ComponentType.AGENT,
                provides=["agent_management", "task_execution", "llm_interaction"],
                dependencies=self._required_dependencies,
                priority=60,
                health_checks={
                    "agent_health": self._check_agent_health,
                    "task_health": self._check_task_health,
                    "cache_health": self._check_cache_health,
                    "dependency_health": self._check_dependency_health,  # Add dependency health check
                    "resource_health": self._check_resource_health  # Add resource health check
                },
                timing_config=self._timing_config,
                cleanup_priority=90,
                tags={"agent", "llm", "automation"},
                is_critical=False
            )
            
            logger.debug("Registered agent manager with component registry")
            
        except Exception as e:
            logger.error(f"Error registering with component registry: {str(e)}")
            raise

    async def _check_agent_health(self) -> tuple[bool, str]:
        """Check the health of the agent manager."""
        try:
            if not self._initialized:
                return False, "Agent manager not initialized"
            if self._agent and not await self._agent.is_ready():
                return False, "Agent not ready"
            return True, "Agent manager healthy"
        except Exception as e:
            return False, f"Error checking agent health: {str(e)}"

    async def _check_task_health(self) -> tuple[bool, str]:
        """Check the health of task processing."""
        try:
            active_tasks = len(self._active_tasks)
            if active_tasks > AGENT_CONFIG['max_concurrent_tasks']:
                return False, f"Too many active tasks: {active_tasks}"
            return True, "Task processing healthy"
        except Exception as e:
            return False, f"Error checking task health: {str(e)}"

    async def _check_cache_health(self) -> tuple[bool, str]:
        """Check the health of agent caches."""
        try:
            if not agent_model_cache.stats.size and not agent_cache.stats.size:
                return False, "Caches not initialized"
            return True, "Caches healthy"
        except Exception as e:
            return False, f"Error checking cache health: {str(e)}"

    async def _check_dependency_health(self) -> tuple[bool, str]:
        """Check health of all dependencies."""
        try:
            unhealthy_deps = []
            for dep in self._required_dependencies:
                component = container.get(dep)
                if not component:
                    unhealthy_deps.append(f"{dep} (missing)")
                    continue
                
                if hasattr(component, 'is_healthy'):
                    is_healthy = await component.is_healthy()
                    if not is_healthy:
                        unhealthy_deps.append(dep)
                
            if unhealthy_deps:
                return False, f"Unhealthy dependencies: {', '.join(unhealthy_deps)}"
            return True, "All dependencies healthy"
        except Exception as e:
            return False, f"Error checking dependency health: {str(e)}"

    async def _check_resource_health(self) -> tuple[bool, str]:
        """
        Check system resource health for agent operations.
        
        Returns:
            tuple[bool, str]: Health status and message
        """
        try:
            concurrency_manager = await self._get_concurrency_manager()
            if not concurrency_manager:
                return False, "Concurrency manager not available"
                
            # Get resource metrics
            metrics = {
                'memory_percent': psutil.Process().memory_percent(),
                'cpu_percent': psutil.Process().cpu_percent(),
                'task_queue_size': len(self._active_tasks),
                'active_threads': len(threading.enumerate()),
                'pending_callbacks': len(asyncio.all_tasks())
            }
            
            # Check against thresholds
            warnings = []
            for metric, value in metrics.items():
                threshold = self._resource_thresholds.get(metric)
                if threshold and value > threshold:
                    warnings.append(f"{metric}: {value:.1f}% (threshold: {threshold}%)")
            
            if warnings:
                return False, f"Resource warnings: {', '.join(warnings)}"
            
            return True, "Resource health check passed"
            
        except Exception as e:
            logger.error(f"Error checking resource health: {str(e)}")
            return False, f"Resource health check failed: {str(e)}"
            
    async def _monitor_resources(self) -> None:
        """Monitor system resources and take action if thresholds are exceeded."""
        while not self._shutdown_event.is_set():
            try:
                is_healthy, message = await self._check_resource_health()
                
                if not is_healthy:
                    logger.warning(f"Resource health check failed: {message}")
                    
                    # Emit resource warning signal
                    signal_manager = await self._get_signal_manager()
                    if signal_manager:
                        await signal_manager.emit(Signal(
                            type=SignalType.AGENT_RESOURCE_WARNING,
                            data={
                                'message': message,
                                'timestamp': datetime.now().isoformat()
                            },
                            source='agent_manager'
                        ))
                    
                    # Take action based on severity
                    if 'memory_percent' in message and float(message.split('memory_percent:')[1].split('%')[0]) > 90:
                        logger.error("Critical memory usage detected, initiating emergency cleanup")
                        await self._emergency_cleanup()
                        
            except Exception as e:
                logger.error(f"Error in resource monitoring: {str(e)}")
                
            await asyncio.sleep(30)  # Check every 30 seconds
            
    async def _emergency_cleanup(self) -> None:
        """Perform emergency cleanup when resources are critically low."""
        try:
            # Cancel non-critical tasks
            tasks_to_cancel = []
            for task in self._active_tasks:
                if not self._is_critical_task(task):
                    tasks_to_cancel.append(task)
                    
            for task in tasks_to_cancel:
                await self._cancel_task(task)
                
            # Force garbage collection
            import gc
            gc.collect()
            
            # Clear caches
            self._clear_caches()
            
            logger.info("Emergency cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during emergency cleanup: {str(e)}")
            
    def _is_critical_task(self, task: str) -> bool:
        """Determine if a task is critical and should not be cancelled."""
        critical_keywords = {'health', 'cleanup', 'shutdown', 'error_recovery'}
        return any(keyword in task.lower() for keyword in critical_keywords)
        
    async def _cancel_task(self, task: str) -> None:
        """Safely cancel a task and clean up its resources."""
        try:
            self._active_tasks.remove(task)
            
            signal_manager = await self._get_signal_manager()
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.AGENT_TASK_CANCELLED,
                    data={
                        'task': task,
                        'reason': 'emergency_cleanup'
                    },
                    source='agent_manager'
                ))
                
            logger.info(f"Cancelled task: {task}")
            
        except Exception as e:
            logger.error(f"Error cancelling task {task}: {str(e)}")
            
    def _clear_caches(self) -> None:
        """Clear internal caches to free memory."""
        try:
            # Clear conversation history cache
            if hasattr(self, '_conversation_history'):
                self._conversation_history.clear()
                
            # Clear LLM response cache
            if hasattr(self, '_llm_cache'):
                self._llm_cache.clear()
                
            # Clear any other internal caches
            self._task_attempts.clear()
            self._task_metrics.clear()
            
            logger.info("Cleared internal caches")
            
        except Exception as e:
            logger.error(f"Error clearing caches: {str(e)}")

    async def _register_with_orchestrator(self):
        """Register agent manager with orchestrator and scheduler."""
        try:
            # Register timing configuration with orchestrator
            await orchestrator.register_handler(
                'agent_manager',
                self._timing_config
            )
            
            # Create initialization task
            init_task = InitializationTask(
                component='agent_manager',
                dependencies={'signal_manager', 'concurrency_manager', 'browser_integration'},
                priority=self._timing_config.priority,
                init_fn=self._initialize_agent_manager,
                timeout=30.0,
                retry_count=3,
                retry_delay=1.0,
                health_check=self._check_agent_health
            )
            
            # Schedule with component scheduler
            await component_scheduler.schedule_component(init_task)
            
            # Register cleanup with concurrency manager's compactor
            concurrency_manager.compactor.register_cleanup_task(
                name="agent_manager_cleanup",
                cleanup_func=self.cleanup,
                priority=90,
                is_async=True,
                metadata={"tags": ["agent", "cleanup"]}
            )
            
        except Exception as e:
            logger.error(f"Failed to register with orchestrator: {str(e)}")

    async def _initialize_agent_manager(self):
        """Initialize agent manager components with cold condition handling."""
        try:
            # Pre-warm the system
            await self._pre_warm_resources()
            
            # Initialize any required resources
            await self._setup_agent_resources()
            
            # Initialize caches if not already done
            if not agent_model_cache.stats.size and not agent_cache.stats.size:
                await self._initialize_caches()
            
            return True
        except Exception as e:
            logger.error(f"Failed to initialize agent manager: {str(e)}")
            return False

    async def _pre_warm_resources(self):
        """Pre-warm resources for better cold start performance."""
        try:
            # Pre-warm caches
            await asyncio.gather(
                self._warm_up_model_cache(),
                self._warm_up_agent_cache(),
                self._warm_up_task_queue()
            )
        except Exception as e:
            logger.warning(f"Resource pre-warm failed (non-critical): {str(e)}")

    async def _warm_up_model_cache(self):
        """Warm up the model cache."""
        try:
            # Pre-load common configurations
            default_config = self._batch_handler._get_llm_config()
            await agent_model_cache.add_model(
                'chatgpt',
                'default_config',
                default_config,
                metadata={'last_updated': datetime.now().isoformat()}
            )
        except Exception as e:
            logger.debug(f"Model cache warm-up failed: {str(e)}")

    async def _warm_up_agent_cache(self):
        """Warm up the agent cache."""
        try:
            # Pre-allocate cache space
            await agent_cache.ensure_capacity(1000)  # 1000 entries
        except Exception as e:
            logger.debug(f"Agent cache warm-up failed: {str(e)}")

    async def _warm_up_task_queue(self):
        """Warm up the task queue."""
        try:
            # Create and immediately process a dummy task
            dummy_task = ('dummy', lambda: None, [], {})
            await self._task_queue.put(dummy_task)
            await self._task_queue.get()
            self._task_queue.task_done()
        except Exception as e:
            logger.debug(f"Task queue warm-up failed: {str(e)}")

    async def _setup_agent_resources(self):
        """Set up required agent resources."""
        # Setup any additional resources needed
        pass

    async def _process_task_queue(self):
        """Process tasks from the queue with concurrency management."""
        while not self._shutdown_event.is_set():
            try:
                task = await self._task_queue.get()
                if task is None:  # Shutdown signal
                    break
                    
                task_id, func, args, kwargs = task
                
                # Execute task with concurrency management
                try:
                    # Run CPU-bound operations in thread pool
                    if kwargs.pop('use_thread_pool', False):
                        result = await concurrency_manager.run_in_thread(func, *args, **kwargs)
                    else:
                        # Run async operations directly
                        result = await func(*args, **kwargs)
                        
                    # Store result
                    self._task_results[task_id] = result
                    
                except Exception as e:
                    logger.error(f"Error processing task {task_id}: {str(e)}")
                    self._task_results[task_id] = {"success": False, "error": str(e)}
                    
                finally:
                    self._task_queue.task_done()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in task processor: {str(e)}")
                await asyncio.sleep(1)  # Brief pause on error

    async def _register_signal_handlers(self):
        """Register signal handlers with proper subgraph integration."""
        try:
            # Get signal manager with retries
            signal_manager = None
            retry_count = 0
            max_retries = 3
            
            while retry_count < max_retries:
                try:
                    signal_manager = await self._get_signal_manager()
                    if signal_manager:
                        break
                except Exception:
                    retry_count += 1
                    if retry_count < max_retries:
                        await asyncio.sleep(2 ** retry_count)
                        
            if not signal_manager:
                logger.error("Failed to get signal manager for handler registration")
                return
                
            # Register core agent handlers
            handlers = [
                (SignalType.AGENT_TASK_STARTED, self._handle_task_started, 10),
                (SignalType.AGENT_TASK_COMPLETED, self._handle_task_completed, 10),
                (SignalType.AGENT_TASK_ERROR, self._handle_task_error, 10),
                (SignalType.AGENT_THOUGHT_GENERATED, self._handle_thought_generated, 5),
                (SignalType.AGENT_MESSAGE, self._handle_agent_message, 5),
                (SignalType.AGENT_RESPONSE, self._handle_agent_response, 5),
                (SignalType.AGENT_STATE_CHANGED, self._handle_agent_state_changed, 5),
                (SignalType.AGENT_CLEANUP_REQUESTED, self._handle_cleanup_request, 10),
                # Add command handlers
                (SignalType.COMMAND_RECEIVED, self._handle_command_received, 10),
                (SignalType.COMMAND_PROCESSED, self._handle_command_processed, 10),
                (SignalType.COMMAND_ERROR, self._handle_command_error, 10),
                (SignalType.BROWSER_COMMAND, self._handle_browser_command, 10),
                (SignalType.BROWSER_ACTION, self._handle_browser_action, 10)
            ]
            
            # Register handlers with proper error handling
            for signal_type, handler, priority in handlers:
                try:
                    signal_manager.register_handler(
                        signal_type,
                        handler,
                        priority=priority
                    )
                    logger.debug(f"Registered handler for {signal_type.name}")
                except Exception as e:
                    logger.error(f"Failed to register handler for {signal_type.name}: {str(e)}")
                    
            # Register with subgraph
            try:
                from tenire.structure.subgraph import subgraph_manager
                gui_subgraph = subgraph_manager.get_subgraph("gui")
                if gui_subgraph:
                    gui_subgraph.add_node(
                        name="agent_manager",
                        dependencies={"signal_manager", "browser_integration"},
                        init_fn=self.initialize_agent,
                        priority=60,
                        metadata={
                            "type": "agent",
                            "provides": ["agent_management", "task_execution"]
                        },
                        health_checks={
                            "agent_health": self._check_agent_health
                        },
                        timing_config=self._timing_config,
                        cleanup_priority=90,
                        tags={"agent", "automation"},
                        is_critical=False
                    )
                    logger.debug("Registered agent manager with GUI subgraph")
            except Exception as e:
                logger.error(f"Failed to register with subgraph: {str(e)}")
                
            logger.info("Successfully registered all agent signal handlers")
            
        except Exception as e:
            logger.error(f"Error during signal handler registration: {str(e)}")
            
    async def _handle_agent_message(self, signal: Signal) -> None:
        """Handle agent message signals."""
        message = signal.data.get("message")
        is_system = signal.data.get("is_system", False)
        if message:
            logger.info(f"{'[System] ' if is_system else ''}{message}")
            
    async def _handle_agent_response(self, signal: Signal) -> None:
        """Handle agent response signals."""
        message = signal.data.get("message")
        if message:
            logger.info(f"Agent response: {message}")
            
    async def _handle_agent_state_changed(self, signal: Signal) -> None:
        """Handle agent state change signals."""
        new_state = signal.data.get("state")
        if new_state:
            logger.debug(f"Agent state changed to: {new_state}")
            
    async def _handle_cleanup_request(self, signal: Signal) -> None:
        """Handle cleanup request signals."""
        try:
            logger.info("Received cleanup request for agent manager")
            await self.cleanup()
        except Exception as e:
            logger.error(f"Error during cleanup request handling: {str(e)}")

    def _setup_logs_directory(self) -> None:
        """Create logs directory if it doesn't exist."""
        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)

    def _create_action_config(self, action_name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Create an action configuration with proper description.
        
        Args:
            action_name: Name of the action to configure
            params: Optional parameters for the action
            
        Returns:
            Dict containing the action configuration
        """
        description = ACTION_DESCRIPTIONS.get(action_name)
        if not description:
            raise ValueError(f"No description found for action: {action_name}")
            
        config = {"description": description}
        if params:
            config["params"] = params
        return config
        
    async def initialize_agent(self, task: str) -> None:
        """Initialize a new agent with the given task with cold condition handling."""
        async with self._task_semaphore:  # Ensure sequential initialization
            if task in self._active_tasks:
                logger.info(f"Task already running: {task}")
                return
                
            try:
                self._active_tasks.add(task)
                self._current_task = task
                self._task_attempts[task] = 0
                
                # Create data pipeline for task
                if not self._data_pipeline:
                    await self._initialize_data_pipeline()
                    
                await self._data_pipeline.create_task_pipeline(
                    task_id=task,
                    data_type=dict  # Use dict as default data type
                )
                
                signal_manager = await self._get_signal_manager()
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.AGENT_TASK_STARTED,
                        data={"task": task},
                        source="initialize_agent"
                    ))
                
                # Wait for any existing browser operations with increased timeout
                if self._agent:
                    try:
                        async with asyncio.timeout(30.0):  # Increased timeout
                            await self._agent.wait_until_done()
                    except asyncio.TimeoutError:
                        logger.warning("Timeout waiting for previous agent operations")
                
                concurrency_manager = await self._get_concurrency_manager()
                if not concurrency_manager:
                    raise RuntimeError("Concurrency manager not available")
                
                # Try to get cached LLM configuration with retry
                llm_config = None
                for attempt in range(3):  # Up to 3 attempts
                    try:
                        cached_config = await agent_model_cache.get_model('chatgpt', 'default_config')
                        if cached_config:
                            llm_config = cached_config[0]
                            break
                    except Exception:
                        await asyncio.sleep(1)
                
                if not llm_config:
                    # Get LLM configuration and cache it
                    llm_config = await concurrency_manager.thread_pool.execute(
                        self._batch_handler._get_llm_config
                    )
                    await agent_model_cache.add_model(
                        'chatgpt',
                        'default_config',
                        llm_config,
                        metadata={'last_updated': datetime.now().isoformat()}
                    )
                
                # Initialize LLM with config and retry on failure
                llm = None
                for attempt in range(3):  # Up to 3 attempts
                    try:
                        llm = ChatOpenAI(**llm_config)
                        break
                    except TypeError as e:
                        if attempt == 2:  # Last attempt
                            logger.warning(f"Failed to initialize LLM with full config: {str(e)}")
                            base_config = {k: v for k, v in llm_config.items() 
                                         if k not in ["model_kwargs"]}
                            llm = ChatOpenAI(**base_config)
                        else:
                            await asyncio.sleep(1)
                
                # Reuse existing browser and context if available
                if self._agent and hasattr(self._agent, 'browser_context'):
                    browser_context = self._agent.browser_context
                else:
                    browser_context = await self._browser_integration.get_context()
                
                # Create the agent with optimized configuration and reused context
                self._agent = Agent(
                    task=task,
                    llm=llm,
                    controller=betting_controller,
                    browser=None,  # Don't pass browser when using context
                    browser_context=browser_context,  # Pass the existing or new context
                    use_vision=AGENT_CONFIG['use_vision'],
                    save_conversation_path=AGENT_CONFIG['save_conversation_path'],
                    initial_actions=AGENT_CONFIG['initial_actions'],
                    max_failures=AGENT_CONFIG['max_failures'] * 2  # Double max failures for cold conditions
                )
                
                # Store the context for reuse
                if browser_context:
                    from tenire.actions import set_shared_context
                    set_shared_context(browser_context)
                
                logger.info(f"Agent initialized with task: {task}")
                
            except Exception as e:
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.AGENT_TASK_ERROR,
                        data={"task": task, "error": str(e)},
                        source="initialize_agent"
                    ))
                logger.error(f"Failed to initialize agent: {str(e)}")
                self._cleanup_task(task)
                raise

    @cache_decorator(agent_cache, key_prefix="agent_task", ttl=3600)
    async def execute_task(self) -> Dict[str, Any]:
        """Execute the current task with caching."""
        signal_manager = await self._get_signal_manager()
        
        try:
            if not self._agent:
                raise ValueError("Agent not initialized")
                
            # Get task from current_task property
            if not self._current_task:
                raise ValueError("No task assigned to agent")
                
            # Get next interval from orchestrator
            interval = await orchestrator.get_next_interval('agent_manager')
            
            # Record operation with orchestrator
            await orchestrator.record_operation('agent_manager')
                
            # Emit task started signal
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.AGENT_TASK_STARTED,
                    data={"task": self._current_task},
                    source="agent_manager"
                ))
            
            # Execute task using run() method with timing control and concurrency management
            result = await concurrency_manager.async_tasks.create_task(
                self._agent.run(),
                name=f"agent_task_{self._current_task}",
                timeout=self._task_timeout
            )
            
            # Process and cache agent output
            output_data = {}
            
            if hasattr(result, 'model_thoughts') and result.model_thoughts():
                thoughts = result.model_thoughts()
                output_data['thoughts'] = thoughts
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.AGENT_MESSAGE,
                        data={"message": f"🤔 {thoughts}", "is_system": True},
                        source="agent_manager"
                    ))
                
            if hasattr(result, 'action') and result.action:
                action = result.action
                output_data['action'] = action
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.AGENT_MESSAGE,
                        data={"message": f"🛠️ Action: {action}", "is_system": True},
                        source="agent_manager"
                    ))
                
            if hasattr(result, 'evaluation') and result.evaluation:
                evaluation = result.evaluation
                output_data['evaluation'] = evaluation
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.AGENT_MESSAGE,
                        data={"message": f"📝 Evaluation: {evaluation}", "is_system": True},
                        source="agent_manager"
                    ))
                
            if hasattr(result, 'error') and result.error:
                error = result.error
                output_data['error'] = error
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.AGENT_TASK_ERROR,
                        data={"task": self._current_task, "error": error},
                        source="agent_manager"
                    ))
                return {"success": False, "error": error}
                
            # Get final result
            final_result = result.final_result if hasattr(result, 'final_result') else str(result)
            output_data['final_result'] = final_result
                
            # Emit task completed signal
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.AGENT_TASK_COMPLETED,
                    data={"task_id": self._current_task, "result": final_result},
                    source="agent_manager"
                ))
            
            # Cache the complete output data
            cache_key = f"task_output:{self._current_task}"
            await agent_cache.set(cache_key, output_data)
            
            return {"success": True, "final_result": final_result}
            
        except Exception as e:
            logger.error(f"Error executing task: {str(e)}")
            if self._current_task and signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.AGENT_TASK_ERROR,
                    data={"task": self._current_task, "error": str(e)},
                    source="agent_manager"
                ))
            return {"success": False, "error": str(e)}

    async def _execute_with_progress_monitoring(
        self,
        task_id: str,
        max_steps: int,
        start_time: float,
        last_progress_time: float,
        progress_timeout: float = AGENT_CONFIG['progress_monitoring']['progress_timeout']
    ) -> Any:
        """
        Execute the agent's task with progress monitoring.
        
        Args:
            task_id: Unique identifier for the task
            max_steps: Maximum number of steps to execute
            start_time: Time when execution started
            last_progress_time: Time of last progress update
            progress_timeout: Maximum time allowed without progress
            
        Returns:
            Agent execution results
            
        Raises:
            TimeoutError: If no progress is made within the timeout period
        """
        steps_taken = 0
        last_action_hash = None
        signal_manager = await self._get_signal_manager()
        
        while steps_taken < max_steps:
            # Get next interval from orchestrator
            interval = await orchestrator.get_next_interval('agent_manager')
            
            # Check for progress timeout
            current_time = asyncio.get_event_loop().time()
            if current_time - last_progress_time > progress_timeout:
                raise TimeoutError("No progress made within timeout period")
            
            # Record operation with orchestrator
            await orchestrator.record_operation('agent_manager')
            
            # Execute one step with timing control and concurrency management
            step_result = await concurrency_manager.async_tasks.create_task(
                self._agent.step(),
                name=f"agent_step_{task_id}_{steps_taken}",
                timeout=self._task_timeout
            )
            steps_taken += 1
            
            # Emit thought generated signal
            if step_result.model_thoughts() and signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.AGENT_THOUGHT_GENERATED,
                    data={
                        "task_id": task_id,
                        "thoughts": step_result.model_thoughts(),
                        "step": steps_taken
                    },
                    source="_execute_with_progress_monitoring"
                ))
            
            # Update progress tracking
            current_action_hash = hash(str(step_result))
            if current_action_hash != last_action_hash:
                last_progress_time = current_time
                last_action_hash = current_action_hash
            
            # Check for completion or errors
            if step_result.is_done() or step_result.has_errors():
                break
            
            # Wait for next interval using concurrency manager's semaphore
            async with concurrency_manager.semaphores.acquire("agent_step_delay"):
                await asyncio.sleep(interval)
        
        # Execute final run with concurrency management
        return await concurrency_manager.async_tasks.create_task(
            self._agent.run(max_steps=max_steps),
            name=f"agent_final_run_{task_id}",
            timeout=self._task_timeout
        )

    def _cleanup_task(self, task: str) -> None:
        """Clean up task-related resources."""
        if task:
            self._active_tasks.discard(task)
            self._task_attempts.pop(task, None)
            if task == self._current_task:
                self._current_task = None

    async def analyze_betting_history(self) -> dict:
        """
        Analyze betting history using the agent's capabilities.
        
        Returns:
            dict: Analysis results including patterns and recommendations
        """
        if not self._agent:
            raise ValueError("Agent not initialized. Call initialize_agent first.")
            
        try:
            analysis_task = (
                "Analyze the betting history and provide insights on: "
                "1. Win/loss patterns "
                "2. Most profitable games "
                "3. Optimal bet sizes "
                "4. Recommendations for future bets"
            )
            
            await self.initialize_agent(analysis_task)
            return await self.execute_task()
            
        except Exception as e:
            logger.error(f"Betting history analysis failed: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }

    async def optimize_betting_strategy(self, game: str) -> dict:
        """
        Use the agent to optimize betting strategy for a specific game.
        
        Args:
            game: The game to optimize strategy for
            
        Returns:
            dict: Optimized strategy recommendations
        """
        if not self._agent:
            raise ValueError("Agent not initialized. Call initialize_agent first.")
            
        try:
            optimization_task = (
                f"Analyze the {game} game and develop an optimal betting strategy considering: "
                "1. Game mechanics and probabilities "
                "2. Historical performance "
                "3. Risk management "
                "4. Specific betting parameters"
            )
            
            await self.initialize_agent(optimization_task)
            return await self.execute_task()
            
        except Exception as e:
            logger.error(f"Strategy optimization failed: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }

    async def cleanup(self) -> None:
        """Clean up agent manager resources."""
        try:
            logger.info("Starting agent manager cleanup")
            
            # Clean up toolkits
            await toolkit_manager.cleanup()
            
            # Signal shutdown
            self._shutdown_event.set()
            
            # Stop task processor
            await self._task_queue.put(None)  # Send shutdown signal
            try:
                await asyncio.wait_for(self._task_queue.join(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for task queue to empty")
            
            # Clean up agent resources
            if self._agent:
                try:
                    await self._agent.cleanup()
                except Exception as e:
                    logger.error(f"Error cleaning up agent: {str(e)}")
                finally:
                    self._agent = None
            
            # Clean up caches with compactor integration
            try:
                await agent_model_cache.cleanup()
                await agent_cache.cleanup()
                
                # Register cache cleanup with compactor
                compactor.register_cleanup_task(
                    name="agent_cache_cleanup",
                    cleanup_func=self._cleanup_caches,
                    priority=85,
                    is_async=True,
                    metadata={"tags": ["agent", "cache", "cleanup"]}
                )
            except Exception as e:
                logger.error(f"Error cleaning up caches: {str(e)}")
            
            # Clean up task-related resources
            self._active_tasks.clear()
            self._task_attempts.clear()
            self._task_results.clear()
            self._current_task = None
            
            # Clean up signal handlers
            try:
                signal_manager = await self._get_signal_manager()
                if signal_manager:
                    # Emit cleanup signal
                    await signal_manager.emit(Signal(
                        type=SignalType.AGENT_CLEANUP_COMPLETED,
                        data={"source": "agent_manager"},
                        source="agent_manager"
                    ))
            except Exception as e:
                logger.error(f"Error cleaning up signal handlers: {str(e)}")
            
            # Clean up event loop resources
            try:
                loop = event_loop_manager.get_event_loop()
                if loop and loop.is_running():
                    # Cancel all pending tasks
                    pending = asyncio.all_tasks(loop)
                    if pending:
                        for task in pending:
                            if not task.done() and task != asyncio.current_task():
                                task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
            except Exception as e:
                logger.error(f"Error cleaning up event loop resources: {str(e)}")
            
            logger.info("Agent manager cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during agent manager cleanup: {str(e)}")
            raise
        finally:
            self._shutdown_event.clear()
            
    async def _cleanup_caches(self) -> None:
        """Clean up agent caches with proper error handling."""
        try:
            # Clean up model cache
            if agent_model_cache:
                try:
                    await agent_model_cache.cleanup()
                    logger.debug("Model cache cleaned up")
                except Exception as e:
                    logger.error(f"Error cleaning up model cache: {str(e)}")
            
            # Clean up agent cache
            if agent_cache:
                try:
                    await agent_cache.cleanup()
                    logger.debug("Agent cache cleaned up")
                except Exception as e:
                    logger.error(f"Error cleaning up agent cache: {str(e)}")
                    
        except Exception as e:
            logger.error(f"Error in cache cleanup: {str(e)}")
            
    def __del__(self):
        """Ensure cleanup on object deletion."""
        try:
            loop = event_loop_manager.get_event_loop()
            if loop and loop.is_running():
                loop.create_task(self.cleanup())
        except Exception as e:
            logger.error(f"Error in __del__: {str(e)}")
            
    async def __aenter__(self):
        """Async context manager entry."""
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit with proper cleanup."""
        try:
            await self.cleanup()
        except Exception as e:
            logger.error(f"Error in __aexit__: {str(e)}")
            
    def register_cleanup_handlers(self) -> None:
        """Register cleanup handlers with the compactor."""
        try:
            # Register main cleanup
            compactor.register_cleanup_task(
                name="agent_manager_cleanup",
                cleanup_func=self.cleanup,
                priority=90,
                is_async=True,
                metadata={"tags": ["agent", "cleanup"]}
            )
            
            # Register cache cleanup
            compactor.register_cleanup_task(
                name="agent_cache_cleanup",
                cleanup_func=self._cleanup_caches,
                priority=85,
                is_async=True,
                metadata={"tags": ["agent", "cache", "cleanup"]}
            )
            
            # Register task cleanup
            compactor.register_cleanup_task(
                name="agent_task_cleanup",
                cleanup_func=self._cleanup_tasks,
                priority=80,
                is_async=True,
                metadata={"tags": ["agent", "task", "cleanup"]}
            )
            
            logger.debug("Registered agent cleanup handlers with compactor")
            
        except Exception as e:
            logger.error(f"Error registering cleanup handlers: {str(e)}")
            
    async def _cleanup_tasks(self) -> None:
        """Clean up task-related resources."""
        try:
            # Clear task collections
            self._active_tasks.clear()
            self._task_attempts.clear()
            self._task_results.clear()
            self._current_task = None
            
            # Clear task queue
            while not self._task_queue.empty():
                try:
                    self._task_queue.get_nowait()
                    self._task_queue.task_done()
                except asyncio.QueueEmpty:
                    break
                    
            logger.debug("Task resources cleaned up")
            
        except Exception as e:
            logger.error(f"Error cleaning up tasks: {str(e)}")

    async def _handle_task_started(self, signal: Signal) -> None:
        """Handle task started signal."""
        task = signal.data.get("task")
        if task:
            self._task_attempts[task] = 0
            logger.info(f"Task started: {task}")

    async def _handle_task_completed(self, signal: Signal) -> None:
        """Handle task completed signal with betting analysis."""
        task_id = signal.data.get("task_id")
        if task_id:
            # Get final betting analysis
            if self._data_pipeline:
                analysis = await self._data_pipeline.get_betting_analysis(task_id)
                if analysis:
                    summary = analysis.get('summary', {})
                    
                    # Emit detailed completion message
                    await self._emit_signal(
                        SignalType.AGENT_RESPONSE,
                        {
                            'message': (
                                f"✅ Task completed: {task_id}\n"
                                f"Total Bets: {summary.get('total_bets', 0)}\n"
                                f"Win Rate: {summary.get('win_rate', 0):.1%}\n"
                                f"Avg Payout: {summary.get('avg_payout', 0):.2f}x\n"
                                f"Longest Win Streak: {summary.get('longest_win_streak', 0)}\n"
                                f"Analysis Window: {summary.get('analysis_window_days', 0)} days"
                            ),
                            'is_system': True
                        }
                    )
                    
                    # Store final results
                    self._task_results[task_id] = {
                        **self._task_results.get(task_id, {}),
                        'final_analysis': analysis,
                        'result': signal.data.get("result")
                    }
            else:
                # Fall back to basic completion message
                self._task_results[task_id] = signal.data.get("result")
                await self._emit_signal(
                    SignalType.AGENT_RESPONSE,
                    {
                        'message': f"✅ {signal.data.get('result', 'Task completed')}",
                        'is_system': True
                    }
                )

    async def _handle_task_error(self, signal: Signal) -> None:
        """Handle task error signal."""
        task = signal.data.get("task")
        error = signal.data.get("error")
        if task:
            self._task_attempts[task] = self._task_attempts.get(task, 0) + 1
            logger.error(f"Task error: {task} - {error}")
            
            signal_manager = await self._get_signal_manager()
            if signal_manager:
                # Emit agent message signal for errors
                await signal_manager.emit(Signal(
                    type=SignalType.AGENT_MESSAGE,
                    data={"message": f"❌ Error: {error}", "is_system": True},
                    source="agent_manager"
                ))

    async def _handle_thought_generated(self, signal: Signal) -> None:
        """Handle thought generated signal."""
        thoughts = signal.data.get("thoughts")
        if thoughts:
            logger.debug(f"Agent thoughts: {thoughts}")
            
            signal_manager = await self._get_signal_manager()
            if signal_manager:
                # Emit agent message signal for thoughts
                await signal_manager.emit(Signal(
                    type=SignalType.AGENT_MESSAGE,
                    data={"message": f"🤔 Thinking: {thoughts}", "is_system": True},
                    source="agent_manager"
                )) 

    async def _initialize_data_pipeline(self) -> None:
        """Initialize the agent data pipeline."""
        try:
            # Get dependencies
            container = await _get_container()
            data_seive = container.get('data_seive')
            data_manager = container.get('data_manager')
            data_flow_agent = container.get('data_flow_agent')
            
            if not all([data_seive, data_manager, data_flow_agent]):
                raise RuntimeError("Missing required data pipeline dependencies")
                
            # Create pipeline
            self._data_pipeline = AgentDataPipeline(
                data_seive=data_seive,
                data_manager=data_manager,
                data_flow_agent=data_flow_agent,
                config=self._pipeline_config
            )
            
            logger.info("Initialized agent data pipeline")
            
        except Exception as e:
            logger.error(f"Failed to initialize data pipeline: {str(e)}")
            raise
            
    async def _process_agent_data(self, task_id: str, data: Any) -> None:
        """Process data through the agent's data pipeline."""
        try:
            if not self._data_pipeline:
                await self._initialize_data_pipeline()
                
            # Get betting context
            betting_context = None
            if isinstance(data, dict) and 'game_type' in data:
                betting_context = {
                    'prev_outcome': self._task_results.get(task_id, {}).get('last_outcome'),
                    'current_streak': self._task_results.get(task_id, {}).get('current_streak', 0)
                }
                
                # Get prediction before processing
                prediction = await self._data_pipeline.predict_bet_outcome(
                    task_id=task_id,
                    game_type=data['game_type'],
                    current_streak=betting_context['current_streak'],
                    metadata=betting_context
                )
                
                if prediction:
                    # Emit prediction signal
                    await self._emit_signal(
                        SignalType.AGENT_MESSAGE,
                        {
                            'message': (
                                f"🎲 Prediction for {data['game_type']}:\n"
                                f"Win Probability: {prediction['win_probability']:.1%}\n"
                                f"Expected Payout: {prediction['expected_payout']:.2f}x\n"
                                f"Confidence: {prediction['confidence']:.1%}"
                            ),
                            'is_system': True
                        }
                    )
                
            await self._data_pipeline.process_agent_data(
                task_id=task_id,
                data=data,
                metadata={
                    'agent_id': id(self._agent),
                    'task_type': self._current_task,
                    'timestamp': datetime.now().isoformat(),
                    'betting_context': betting_context
                }
            )
            
            # Get and store betting analysis
            if betting_context is not None:
                analysis = await self._data_pipeline.get_betting_analysis(task_id)
                if analysis:
                    self._task_results[task_id] = {
                        **self._task_results.get(task_id, {}),
                        'betting_analysis': analysis,
                        'last_outcome': data.get('payoutMultiplier', 0) > 0,
                        'current_streak': betting_context['current_streak'] + (1 if data.get('payoutMultiplier', 0) > 0 else -1)
                    }
            
        except Exception as e:
            logger.error(f"Error processing agent data: {str(e)}")
            await self._emit_signal(
                SignalType.DATA_FLOW_ERROR,
                {
                    'task_id': task_id,
                    'error': str(e),
                    'stage': 'agent_processing'
                }
            ) 

    async def _initialize_toolkits(self) -> None:
        """Initialize and register available toolkits."""
        try:
            # Register betting toolkit
            betting_toolkit = BettingToolkit()
            await toolkit_manager.register_toolkit(betting_toolkit)
            
            # Additional toolkits can be registered here
            
            logger.info("Initialized agent toolkits")
            
        except Exception as e:
            logger.error(f"Error initializing toolkits: {str(e)}")
            raise
            
    async def process_command(self, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a command using the appropriate toolkit.
        
        Args:
            command: The command to process
            params: Parameters for the command
            
        Returns:
            Dict containing the command results
        """
        try:
            # Execute command through toolkit manager
            result = await toolkit_manager.execute_command(command, params)
            
            # Emit command result signal
            signal_manager = await self._get_signal_manager()
            if signal_manager:
                signal_type = (
                    SignalType.COMMAND_COMPLETED if result.get("success")
                    else SignalType.COMMAND_ERROR
                )
                await signal_manager.emit(Signal(
                    type=signal_type,
                    data={
                        "command": command,
                        "result": result
                    },
                    source="agent_manager"
                ))
                
            return result
            
        except Exception as e:
            logger.error(f"Error processing command {command}: {str(e)}")
            return {
                "success": False,
                "error": f"Command processing failed: {str(e)}"
            } 

    async def _handle_command_received(self, signal: Signal) -> None:
        """Handle received command signals."""
        command = signal.data.get("command")
        params = signal.data.get("params", {})
        
        if command in self._command_handlers:
            try:
                handler = self._command_handlers[command]
                result = await handler(**params)
                
                # Emit command processed signal
                signal_manager = await self._get_signal_manager()
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.COMMAND_PROCESSED,
                        data={
                            "command": command,
                            "result": result,
                            "success": True
                        },
                        source="agent_manager"
                    ))
            except Exception as e:
                logger.error(f"Error processing command {command}: {str(e)}")
                await self._emit_command_error(command, str(e))
        else:
            error = f"Unknown command: {command}"
            logger.error(error)
            await self._emit_command_error(command, error)

    async def _handle_command_processed(self, signal: Signal) -> None:
        """Handle processed command signals."""
        command = signal.data.get("command")
        result = signal.data.get("result")
        if command and result:
            logger.info(f"Command {command} processed: {result}")

    async def _handle_command_error(self, signal: Signal) -> None:
        """Handle command error signals."""
        command = signal.data.get("command")
        error = signal.data.get("error")
        if command and error:
            logger.error(f"Command {command} failed: {error}")

    async def _handle_browser_command(self, signal: Signal) -> None:
        """Handle browser-specific command signals."""
        command = signal.data.get("command")
        params = signal.data.get("params", {})
        
        try:
            # Get browser integration instance
            browser_integration = container.get('browser_integration')
            if not browser_integration:
                raise ValueError("Browser integration not available")
                
            # Execute browser command
            result = await browser_integration.execute_command(command, params)
            
            # Emit result signal
            signal_manager = await self._get_signal_manager()
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.BROWSER_COMMAND_COMPLETED,
                    data={
                        "command": command,
                        "result": result,
                        "success": True,
                        "browser_state": await browser_integration.get_state()
                    },
                    source="agent_manager"
                ))
        except Exception as e:
            logger.error(f"Browser command {command} failed: {str(e)}")
            await self._emit_command_error(command, str(e))

    async def _handle_browser_action(self, signal: Signal) -> None:
        """Handle browser action signals."""
        action = signal.data.get("action")
        params = signal.data.get("params", {})
        
        try:
            # Get browser integration instance
            browser_integration = container.get('browser_integration')
            if not browser_integration:
                raise ValueError("Browser integration not available")
                
            # Execute browser action
            result = await browser_integration.execute_action(action, params)
            
            # Emit result signal
            signal_manager = await self._get_signal_manager()
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.BROWSER_ACTION_COMPLETED,
                    data={
                        "action": action,
                        "result": result,
                        "success": True,
                        "browser_state": await browser_integration.get_state()
                    },
                    source="agent_manager"
                ))
        except Exception as e:
            logger.error(f"Browser action {action} failed: {str(e)}")
            await self._emit_command_error(action, str(e))

    async def _emit_command_error(self, command: str, error: str) -> None:
        """Emit command error signal."""
        signal_manager = await self._get_signal_manager()
        if signal_manager:
            await signal_manager.emit(Signal(
                type=SignalType.COMMAND_ERROR,
                data={
                    "command": command,
                    "error": error,
                    "success": False
                },
                source="agent_manager"
            ))

    async def _handle_browser_action_command(self, **params) -> Dict[str, Any]:
        """Handle browser action commands."""
        action = params.get("action")
        if not action:
            raise ValueError("No browser action specified")
            
        if not self._browser_integration:
            raise ValueError("Browser integration not available")
            
        return await self._browser_integration.execute_action(action, params)