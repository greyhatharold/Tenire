"""
Health monitoring system for the Tenire framework.

This package provides comprehensive health monitoring for framework components,
integrating with the dependency graph to track component states, detect issues,
and provide diagnostic information.
"""

import asyncio
from typing import Optional
from tenire.public_services.hospital.charts import (
    HealthStatus,
    HealthCheckLevel,
    AutoResponseAction,
    HealthCheckResult,
    HealthCheck,
    AutoResponse
)
from tenire.public_services.hospital.registry import HealthCheckRegistry
from tenire.public_services.hospital.executor import HealthCheckExecutor
from tenire.public_services.hospital.responder import ResponseExecutor
from tenire.public_services.hospital.coordinator import HealthCoordinator
from tenire.utils.logger import get_logger

logger = get_logger(__name__)

# Lazy initialization of health coordinator
_health_coordinator = None
_init_lock = asyncio.Lock()

async def get_health_coordinator() -> Optional[HealthCoordinator]:
    """Get or create the global health coordinator instance with non-blocking initialization."""
    global _health_coordinator
    
    if _health_coordinator is not None:
        return _health_coordinator
        
    async with _init_lock:
        # Double check pattern to prevent race conditions
        if _health_coordinator is not None:
            return _health_coordinator
            
        try:
            async with asyncio.timeout(5.0):  # 5 second timeout
                _health_coordinator = HealthCoordinator()
                # Register with container
                from tenire.core.container import container
                container.register_sync('health_coordinator', _health_coordinator)
                logger.debug("Initialized global health coordinator")
                return _health_coordinator
        except asyncio.TimeoutError:
            logger.warning("Health coordinator initialization timed out, will retry later")
            return None
        except Exception as e:
            logger.warning(f"Failed to initialize health coordinator: {str(e)}")
            return None

def get_health_coordinator_sync() -> Optional[HealthCoordinator]:
    """Synchronous accessor for the health coordinator.
    
    This should only be used when async operations are not possible.
    Prefer using the async get_health_coordinator() when possible.
    """
    return _health_coordinator

__all__ = [
    'HealthStatus',
    'HealthCheckLevel',
    'AutoResponseAction',
    'HealthCheckResult',
    'HealthCheck',
    'AutoResponse',
    'HealthCheckRegistry',
    'HealthCheckExecutor',
    'ResponseExecutor',
    'HealthCoordinator',
    'get_health_coordinator',
    'get_health_coordinator_sync'
] 