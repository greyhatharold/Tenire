"""
Concurrency management package for the Tenire framework.

This package provides a centralized system for managing concurrency across the framework,
including thread pools, async tasks, and semaphores. It follows SOLID principles
and provides a clean interface for concurrent operations.
"""

from concurrent.futures import ThreadPoolExecutor
from .types import TaskResult, AbstractTaskResult
from .concurrency_manager import ConcurrencyManager, AbstractConcurrencyManager, concurrency_manager
from .data_manager import DataManager, AbstractDataManager
from .thread_pool_manager import ThreadPoolManager, AbstractThreadPoolManager
from .async_task_manager import AsyncTaskManager, AbstractAsyncTaskManager
from .semaphore_manager import SemaphoreManager, AbstractSemaphoreManager
from .gui_process import GUIProcess, AbstractGUIProcess
from .monitor import ThreadedMonitor, AbstractMonitor, MonitorMetrics, monitor
from ..scheduler import UniversalTimer, TimingConfig, Orchestrator, orchestrator, universal_timer
from tenire.core.container import container
from tenire.utils.logger import get_logger

logger = get_logger(__name__)

async def initialize_concurrency() -> None:
    """
    Initialize the concurrency system.
    
    This function sets up all concurrency-related components in the correct order:
    1. Thread pool manager
    2. Async task manager
    3. Semaphore manager
    4. Data manager
    5. GUI process manager (if needed)
    6. Universal timer and orchestrator
    """
    try:
        logger.info("Initializing concurrency system")
        
        # Initialize managers if not already done
        if not concurrency_manager.is_initialized:
            await concurrency_manager.initialize()
            
        # Start universal timer and orchestrator
        await universal_timer.start()
        await orchestrator.start()
            
        # Register with container
        await container.register(
            'concurrency_manager',
            concurrency_manager,
            dependencies={'signal_manager', 'orchestrator'}
        )
        
        logger.info("Concurrency system initialized successfully")
        
    except Exception as e:
        logger.error(f"Error initializing concurrency system: {str(e)}")
        raise

# Create and expose the singleton instances
__all__ = [
    # Abstract base classes
    'AbstractTaskResult',
    'AbstractConcurrencyManager',
    'AbstractDataManager',
    'AbstractThreadPoolManager',
    'AbstractAsyncTaskManager',
    'AbstractSemaphoreManager',
    'AbstractGUIProcess',
    'AbstractMonitor',
    
    # Concrete implementations
    'TaskResult',
    'ConcurrencyManager',
    'concurrency_manager',  # The singleton instance
    'DataManager',
    'ThreadPoolManager',
    'AsyncTaskManager',
    'SemaphoreManager',
    'GUIProcess',
    'ThreadPoolExecutor',
    'ThreadedMonitor',
    'MonitorMetrics',
    'monitor',
    'UniversalTimer',
    'TimingConfig',
    'Orchestrator',
    'orchestrator',
    'universal_timer',
    'initialize_concurrency'
]

# Expose the singleton instance directly
ConcurrencyManager = concurrency_manager  # This makes it available as tenire.organizers.concurrency.concurrency_manager
