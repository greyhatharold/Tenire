"""
Health check registry for managing health checks and automated responses.

This module provides a clean interface for registering and managing health
checks and their associated automated responses.
"""

import asyncio
from datetime import timedelta
from typing import Dict, List, Callable, Any, Optional

from tenire.utils.logger import get_logger
from tenire.public_services.hospital.charts import (
    HealthCheck,
    HealthCheckResult,
    HealthCheckLevel,
    AutoResponse,
    AutoResponseAction,
)

logger = get_logger(__name__)

class HealthCheckRegistry:
    """
    Registry for health checks and automated responses.
    
    This class follows the Single Responsibility Principle by focusing solely
    on managing the registration and lookup of health checks and responses.
    """
    
    def __init__(self):
        """Initialize the registry."""
        self._health_checks: Dict[str, Dict[str, HealthCheck]] = {}
        self._auto_responses: Dict[str, Dict[str, AutoResponse]] = {}
        self._lock = asyncio.Lock()
        
    def register_check(
        self,
        component: str,
        name: str,
        check_fn: Callable[[], Any],
        interval: timedelta,
        level: HealthCheckLevel = HealthCheckLevel.INFO,
        timeout: float = 5.0
    ) -> None:
        """
        Register a health check for a component.
        
        Args:
            component: Component name
            name: Check name
            check_fn: Check function
            interval: Check interval
            level: Check level
            timeout: Check timeout in seconds
        """
        if component not in self._health_checks:
            self._health_checks[component] = {}
            
        self._health_checks[component][name] = HealthCheck(
            name=name,
            check_fn=check_fn,
            interval=interval,
            level=level,
            timeout=timeout,
            is_async=asyncio.iscoroutinefunction(check_fn)
        )
        
        logger.debug(f"Registered health check: {component}.{name}")
        
    def register_auto_response(
        self,
        component: str,
        check_name: str,
        action: AutoResponseAction,
        conditions: List[Callable[[HealthCheckResult], bool]],
        max_attempts: int = 3,
        cooldown: timedelta = timedelta(minutes=5)
    ) -> None:
        """
        Register an automated response for a health check.
        
        Args:
            component: Component name
            check_name: Name of the check to respond to
            action: Response action to take
            conditions: Conditions that must be met to trigger the response
            max_attempts: Maximum number of response attempts
            cooldown: Cooldown period between attempts
        """
        if component not in self._auto_responses:
            self._auto_responses[component] = {}
            
        self._auto_responses[component][check_name] = AutoResponse(
            action=action,
            conditions=conditions,
            max_attempts=max_attempts,
            cooldown=cooldown,
            is_async=True
        )
        
        logger.debug(f"Registered auto response: {component}.{check_name}")
        
    def get_component_checks(self, component: str) -> Dict[str, HealthCheck]:
        """Get all health checks for a component."""
        return self._health_checks.get(component, {})
        
    def get_component_responses(self, component: str) -> Dict[str, AutoResponse]:
        """Get all automated responses for a component."""
        return self._auto_responses.get(component, {})
        
    def get_check(self, component: str, name: str) -> Optional[HealthCheck]:
        """Get a specific health check."""
        return self._health_checks.get(component, {}).get(name)
        
    def get_response(self, component: str, check_name: str) -> Optional[AutoResponse]:
        """Get a specific automated response."""
        return self._auto_responses.get(component, {}).get(check_name)
        
    def get_all_components(self) -> List[str]:
        """Get all registered component names."""
        return list(set(self._health_checks.keys()) | set(self._auto_responses.keys())) 