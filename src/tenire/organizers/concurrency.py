"""
Concurrency management package for the Tenire framework.

This module provides a centralized system for managing concurrency across the framework,
including thread pools, async tasks, and semaphores. It follows SOLID principles
and provides a clean interface for concurrent operations.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Set, Callable, TypeVar
from abc import ABC, abstractmethod

from .concurrency.types import TaskResult, AbstractTaskResult
from .concurrency.concurrency_manager import ConcurrencyManager, AbstractConcurrencyManager, concurrency_manager
from .concurrency.data_manager import DataManager, AbstractDataManager
from .concurrency.thread_pool_manager import ThreadPoolManager, AbstractThreadPoolManager
from .concurrency.async_task_manager import AsyncTaskManager, AbstractAsyncTaskManager
from .concurrency.semaphore_manager import SemaphoreManager, AbstractSemaphoreManager
from .concurrency.gui_process import GUIProcess, AbstractGUIProcess
from .scheduler import component_scheduler, InitializationTask
from tenire.core.container import container
from tenire.utils.logger import get_logger
from tenire.structure.subgraph import subgraph_manager
from tenire.public_services.hospital.charts import HealthStatus

T = TypeVar('T')

logger = get_logger(__name__)

async def initialize_concurrency() -> None:
    """
    Initialize the concurrency system.
    
    This function sets up all concurrency-related components in the correct order:
    1. Thread pool manager
    2. Async task manager
    3. Semaphore manager
    4. Data manager
    5. GUI process manager
    6. Browser integration
    """
    try:
        logger.info("Initializing concurrency system")
        
        # Initialize managers if not already done
        if not concurrency_manager.is_initialized:
            await concurrency_manager.initialize()
            
        # Register with container
        await container.register(
            'concurrency_manager',
            concurrency_manager,
            dependencies={'signal_manager'}
        )
        
        # Schedule GUI and browser components
        await _schedule_gui_browser_components()
        
        # Initialize components using scheduler
        if not await component_scheduler.initialize_components():
            raise RuntimeError("Failed to initialize scheduled components")
            
        logger.info("Concurrency system initialized successfully")
        
    except Exception as e:
        logger.error(f"Error initializing concurrency system: {str(e)}")
        raise

async def _schedule_gui_browser_components() -> None:
    """Schedule GUI and browser components for initialization."""
    # Get GUI subgraph
    gui_subgraph = subgraph_manager.get_subgraph("gui")
    if not gui_subgraph:
        raise RuntimeError("GUI subgraph not found")
        
    # Schedule GUI initialization
    await component_scheduler.schedule_component(
        InitializationTask(
            component="gui_process",
            dependencies={"signal_manager", "concurrency_manager"},
            priority=80,
            init_fn=lambda: concurrency_manager.start_gui(width=400),
            timeout=60.0,  # GUI might take longer to start
            retry_count=3,
            retry_delay=2.0,
            health_check=_check_gui_health
        )
    )
    
    # Schedule browser initialization
    await component_scheduler.schedule_component(
        InitializationTask(
            component="browser_integration",
            dependencies={"signal_manager", "concurrency_manager", "gui_process"},
            priority=70,
            init_fn=_initialize_browser,
            timeout=90.0,  # Browser might take even longer
            retry_count=3,
            retry_delay=3.0,
            health_check=_check_browser_health
        )
    )

async def _initialize_browser() -> None:
    """Initialize browser integration."""
    browser_integration = container.get('browser_integration')
    if not browser_integration:
        raise RuntimeError("Browser integration not available")
    await browser_integration.start_browser_session()

async def _check_gui_health() -> HealthStatus:
    """Check GUI health status."""
    gui_state = concurrency_manager.gui_state
    if not gui_state.get('is_ready', False):
        return HealthStatus.UNHEALTHY
    return HealthStatus.HEALTHY

async def _check_browser_health() -> HealthStatus:
    """Check browser health status."""
    browser_integration = container.get('browser_integration')
    if not browser_integration:
        return HealthStatus.UNKNOWN
    
    is_ready = await browser_integration.check_browser_ready()
    return HealthStatus.HEALTHY if is_ready else HealthStatus.UNHEALTHY

class AbstractConcurrencyManager(ABC):
    """Abstract base class for concurrency management."""
    
    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the concurrency manager and its dependencies."""
        pass
        
    @abstractmethod
    async def ensure_initialized(self) -> None:
        """Ensure dependencies are initialized."""
        pass
        
    @abstractmethod
    def configure_thread_pool(self, max_workers: Optional[int] = None) -> None:
        """Configure the thread pool."""
        pass
        
    @abstractmethod
    async def start_gui(self, width: int) -> None:
        """Start the GUI process."""
        pass
        
    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up all concurrency-related resources."""
        pass
        
    @abstractmethod
    async def run_in_thread(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Run a function in a thread pool."""
        pass

__all__ = [
    # Abstract base classes
    'AbstractTaskResult',
    'AbstractConcurrencyManager',
    'AbstractDataManager',
    'AbstractThreadPoolManager',
    'AbstractAsyncTaskManager',
    'AbstractSemaphoreManager',
    'AbstractGUIProcess',
    
    # Concrete implementations
    'TaskResult',
    'ConcurrencyManager',
    'concurrency_manager',
    'DataManager',
    'ThreadPoolManager',
    'AsyncTaskManager',
    'SemaphoreManager',
    'GUIProcess',
    'ThreadPoolExecutor',
    'component_scheduler',
    
    # Initialization function
    'initialize_concurrency'
]