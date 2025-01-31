"""
Main entry point for the Tenire framework.

This module initializes all core components in the correct order using
the dependency graph and starts the application.
"""

import asyncio
import logging
import sys
import threading
try:
    from qasync import QEventLoop, QEventLoopPolicy
except ImportError:
    QEventLoop = None
    QEventLoopPolicy = None
    
from PySide6.QtWidgets import QApplication

from tenire.core.config import config_manager
from tenire.core.container import container
from tenire.structure.dependency_graph import dependency_graph, initialize_dependency_graph
from tenire.structure.subgraph import subgraph_manager
from tenire.structure.component_registry import pipeline_registry
from tenire.public_services.doctor import component_doctor
from tenire.servicers.signal_servicer import ensure_signal_manager
from tenire.organizers.concurrency import initialize_concurrency, concurrency_manager
from tenire.data.cacher import initialize_caches
from tenire.public_services.hospital.charts import HealthStatus
from tenire.utils.logger import get_logger
from tenire.organizers.compactor import compactor
from tenire.core.event_loop import import_qasync, event_loop_manager
from tenire.structure.component_scheduler import InitializationTask, component_scheduler

def setup_logging():
    """Configure logging for the application."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('tenire.log')
        ]
    )

setup_logging()
logger = get_logger(__name__)

async def initialize_core_components() -> bool:
    """Initialize core components in the correct order."""
    try:
        # Initialize configuration first
        logger.info("Initializing configuration")
        config_manager.initialize()
        
        # Initialize event loop first
        logger.info("Ensuring event loop is initialized")
        if not await event_loop_manager.ensure_initialized():
            logger.error("Failed to initialize event loop")
            return False
            
        # Get the event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = event_loop_manager.get_event_loop()
            if not loop:
                logger.error("No event loop available")
                return False
            asyncio.set_event_loop(loop)
            
        # Initialize signal manager
        logger.info("Initializing signal manager")
        if not await ensure_signal_manager():
            logger.error("Failed to initialize signal manager")
            return False
            
        # Initialize concurrency system through concurrency manager
        logger.info("Initializing concurrency system")
        try:
            concurrency_manager = container.get('concurrency_manager')
            if not concurrency_manager:
                logger.error("Failed to get concurrency manager")
                return False
                
            await concurrency_manager.ensure_initialized()
        except Exception as e:
            logger.error(f"Failed to initialize concurrency system: {str(e)}")
            return False
            
        # Initialize compactor
        logger.info("Initializing compactor")
        await container.register('compactor', compactor)
        
        # Initialize dependency graph
        logger.info("Initializing dependency graph")
        await initialize_dependency_graph()
        
        # Register core cleanup tasks
        compactor.register_cleanup_task(
            name="dependency_graph_cleanup",
            cleanup_func=dependency_graph.cleanup,
            priority=95,
            is_async=True,
            metadata={"tags": ["core", "dependency_graph"]}
        )
        
        return True
    except Exception as e:
        logger.error(f"Error initializing core components: {str(e)}")
        return False

async def initialize_framework() -> bool:
    """Initialize the framework using the dependency graph and concurrency system."""
    try:
        logger.info("Starting framework initialization")
        
        # Initialize core components first
        if not await initialize_core_components():
            logger.error("Failed to initialize core components")
            return False
            
        # Initialize pipeline registry with proper dependencies
        logger.info("Initializing pipeline registry")
        await pipeline_registry.initialize()
        
        # Register core components with proper dependencies and concurrency
        await _register_core_components()
        
        # Initialize components using scheduler with proper batching and concurrency
        logger.info("Initializing components through scheduler")
        success = await component_scheduler.initialize_components()
        if not success:
            logger.error("Failed to initialize scheduled components")
            return False
            
        # Initialize remaining systems in background using concurrency manager
        logger.info("Starting background initialization")
        loop = asyncio.get_event_loop()
        init_task = await concurrency_manager.async_tasks.create_task(
            initialize_remaining_systems(),
            task_id="background_initialization"
        )
        
        logger.info("Framework initialization completed successfully")
        return True
        
    except Exception as e:
        logger.error(f"Error initializing framework: {str(e)}")
        return False

async def _register_core_components() -> None:
    """Register core components with proper dependencies and concurrency."""
    try:
        # Register signal manager first (no dependencies)
        await component_scheduler.schedule_component(
            InitializationTask(
                component="signal_manager",
                dependencies=set(),  # No dependencies
                priority=1000,  # Highest priority
                init_fn=ensure_signal_manager,
                timeout=30.0,
                retry_count=3,
                retry_delay=1.0,
                metadata={"type": "core", "is_critical": True}
            )
        )

        # Register event loop manager (no dependencies)
        await component_scheduler.schedule_component(
            InitializationTask(
                component="event_loop_manager",
                dependencies=set(),  # No dependencies
                priority=1000,  # Highest priority
                init_fn=event_loop_manager._initialize_loop,
                timeout=30.0,
                retry_count=3,
                retry_delay=1.0,
                health_check=lambda: event_loop_manager.get_event_loop() is not None,
                metadata={"type": "core", "is_critical": True}
            )
        )
        
        # Register async task manager (depends on event_loop_manager)
        await component_scheduler.schedule_component(
            InitializationTask(
                component="async_task_manager",
                dependencies={"event_loop_manager"},
                priority=95,
                init_fn=lambda: concurrency_manager.async_tasks.initialize(),
                timeout=30.0,
                retry_count=3,
                retry_delay=1.0,
                metadata={"type": "core", "is_critical": True}
            )
        )
        
        # Register compactor (depends on signal_manager)
        await component_scheduler.schedule_component(
            InitializationTask(
                component="compactor",
                dependencies={"signal_manager"},
                priority=90,
                init_fn=lambda: container.register('compactor', compactor),
                timeout=30.0,
                retry_count=3,
                retry_delay=1.0,
                metadata={"type": "core", "is_critical": True}
            )
        )
        
        # Register thread pool manager (depends on async_task_manager)
        await component_scheduler.schedule_component(
            InitializationTask(
                component="thread_pool_manager",
                dependencies={"async_task_manager"},
                priority=85,
                init_fn=lambda: concurrency_manager.thread_pool.initialize(),
                timeout=30.0,
                retry_count=3,
                retry_delay=1.0,
                metadata={"type": "core", "is_critical": True}
            )
        )
        
        # Register data manager (depends on thread_pool_manager)
        await component_scheduler.schedule_component(
            InitializationTask(
                component="data_manager",
                dependencies={"thread_pool_manager"},
                priority=80,
                init_fn=lambda: concurrency_manager.data_manager.initialize(),
                timeout=30.0,
                retry_count=3,
                retry_delay=1.0,
                metadata={"type": "core", "is_critical": True}
            )
        )
        
        # Register monitor with updated dependencies
        await component_scheduler.schedule_component(
            InitializationTask(
                component="monitor",
                dependencies={"signal_manager", "compactor"},
                priority=75,
                init_fn=lambda: container.get('monitor').start(),
                timeout=30.0,
                retry_count=3,
                retry_delay=1.0,
                metadata={"type": "core", "is_critical": False}
            )
        )
        
        # Register GUI components with all required dependencies
        await component_scheduler.schedule_component(
            InitializationTask(
                component="gui_process",
                dependencies={
                    "signal_manager",
                    "event_loop_manager",
                    "async_task_manager",
                    "compactor",
                    "thread_pool_manager"
                },
                priority=70,
                init_fn=lambda: concurrency_manager.start_gui(width=400),
                timeout=60.0,
                retry_count=3,
                retry_delay=2.0,
                metadata={
                    "type": "gui",
                    "requires_main_thread": True,
                    "requires_qt": True
                }
            )
        )
        
        # Register browser components last
        await component_scheduler.schedule_component(
            InitializationTask(
                component="browser_integration",
                dependencies={
                    "gui_process",
                    "signal_manager",
                    "event_loop_manager"
                },
                priority=65,
                init_fn=lambda: container.get('browser_integration').start_browser_session(),
                timeout=60.0,
                retry_count=3,
                retry_delay=2.0,
                metadata={"type": "integration"}
            )
        )
        
    except Exception as e:
        logger.error(f"Error registering core components: {str(e)}")
        raise

async def initialize_remaining_systems() -> None:
    """Initialize remaining systems using concurrency manager."""
    try:
        # Initialize caches using thread pool
        await concurrency_manager.thread_pool.run_in_thread(initialize_caches)
        
        # Initialize subgraphs with proper concurrency
        if not await subgraph_manager.initialize_subgraphs():
            logger.error("Failed to initialize subgraphs")
            return
            
        # Get initialization status
        status = dependency_graph.get_initialization_status()
        subgraph_status = subgraph_manager.get_initialization_status()
        
        logger.info(f"Initialization status: {status}")
        logger.info(f"Subgraph status: {subgraph_status}")
        
        # Start health monitoring using async task manager
        health_coordinator = container.get('health_coordinator')
        if health_coordinator:
            await concurrency_manager.async_tasks.create_task(
                health_coordinator.start_monitoring(),
                task_id="health_monitoring"
            )
            
            # Verify core components are healthy
            system_health = await component_doctor.get_system_health()
            if system_health['overall_status'] == HealthStatus.UNHEALTHY:
                logger.error("Core components unhealthy after initialization")
                return
            
            # Verify critical components
            critical_components = ['gui_process', 'browser_integration', 'signal_manager']
            for component in critical_components:
                component_health = system_health['components'].get(component, {})
                if component_health.get('status') != HealthStatus.HEALTHY:
                    logger.error(f"Critical component {component} not healthy: {component_health.get('status')}")
                    return
                    
    except Exception as e:
        logger.error(f"Error in background initialization: {str(e)}")
        return

async def cleanup_framework() -> None:
    """Clean up framework resources using the compactor."""
    try:
        logger.info("Starting framework cleanup")
        
        # Use compactor for cleanup with proper concurrency
        await compactor.cleanup()
        
        # Get current event loop
        try:
            loop = asyncio.get_event_loop()
            if isinstance(loop, QEventLoop):
                # Special handling for Qt event loop
                loop.stop()
                # Don't close Qt event loop as it's managed by Qt
            else:
                # Standard event loop cleanup with proper task handling
                tasks = asyncio.all_tasks(loop)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await loop.shutdown_asyncgens()
                loop.close()
        except Exception as e:
            logger.error(f"Error cleaning up event loop: {str(e)}")
        
    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}")
        # Try direct cleanup as fallback
        try:
            # Get health coordinator
            health_coordinator = container.get('health_coordinator')
            if health_coordinator:
                await health_coordinator.stop_monitoring()
            
            # Clean up GUI first using concurrency manager
            await concurrency_manager.cleanup_gui()
            
            # Clean up browser resources
            await concurrency_manager.cleanup_browser()
            
            # Clean up pipeline registry
            await pipeline_registry.cleanup()
            
            # Clean up components
            await dependency_graph.cleanup()
            
        except Exception as fallback_error:
            logger.error(f"Error during fallback cleanup: {str(fallback_error)}")

async def main():
    """Main entry point with proper event loop and signal handling."""
    try:
        # Initialize framework with proper concurrency
        success = await initialize_framework()
        
        if not success:
            logger.error("Framework initialization failed")
            await cleanup_framework()
            return
            
        # Keep the main loop running with proper signal handling
        logger.info("Framework initialized, running event loop")
        try:
            # Create an event to keep the loop running
            shutdown_event = asyncio.Event()
            
            def handle_shutdown():
                logger.info("Received shutdown signal")
                shutdown_event.set()
            
            # Register signal handlers using concurrency manager
            import signal
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, handle_shutdown)
            
            # Wait for shutdown signal
            await shutdown_event.wait()
            
        except Exception as e:
            logger.error(f"Error in main event loop: {str(e)}")
        finally:
            # Clean up on exit
            logger.info("Starting framework cleanup")
            await cleanup_framework()
        
    except Exception as e:
        logger.error(f"Critical error in main: {str(e)}")
        raise
    finally:
        # Final cleanup
        try:
            await cleanup_framework()
        except Exception as e:
            logger.error(f"Error during final cleanup: {str(e)}")

if __name__ == "__main__":
    try:
        # Initialize logging first
        setup_logging()
        
        # Create and set event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Run main with proper exception handling
            loop.run_until_complete(main())
        finally:
            # Clean up the loop
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
            finally:
                loop.close()
                
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down")
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        sys.exit(1) 