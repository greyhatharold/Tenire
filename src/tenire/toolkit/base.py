"""
Base toolkit interface for the Tenire framework.

This module defines the base interface that all toolkits must implement,
providing a consistent way to extend agent capabilities.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

from tenire.core.codex import DataFlowStatus, DataFlowMetrics

class ToolkitInterface(ABC):
    """Base interface that all toolkits must implement."""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Get the name of this toolkit."""
        pass
        
    @property
    @abstractmethod
    def description(self) -> str:
        """Get a description of this toolkit's capabilities."""
        pass
        
    @property
    @abstractmethod
    def supported_commands(self) -> Dict[str, str]:
        """Get a mapping of supported command names to their descriptions."""
        pass
        
    @abstractmethod
    async def execute_command(self, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a command supported by this toolkit.
        
        Args:
            command: The command to execute
            params: Parameters for the command
            
        Returns:
            Dict containing the execution results
        """
        pass
        
    @abstractmethod
    async def validate_command(self, command: str, params: Dict[str, Any]) -> bool:
        """
        Validate if a command and its parameters are valid for this toolkit.
        
        Args:
            command: The command to validate
            params: Parameters to validate
            
        Returns:
            True if valid, False otherwise
        """
        pass
        
    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up any resources used by this toolkit."""
        pass
        
    async def check_health(self) -> bool:
        """
        Check the health status of this toolkit.
        
        Returns:
            True if healthy, False otherwise
        """
        try:
            # Basic health check - override for more specific checks
            return True
        except Exception:
            return False
            
    async def get_metrics(self) -> Optional[DataFlowMetrics]:
        """
        Get metrics for this toolkit's operations.
        
        Returns:
            Optional metrics data
        """
        return None
        
    async def get_status(self) -> Tuple[DataFlowStatus, str]:
        """
        Get the current status of this toolkit.
        
        Returns:
            Tuple of (status, description)
        """
        try:
            is_healthy = await self.check_health()
            if is_healthy:
                return DataFlowStatus.HEALTHY, "Toolkit operating normally"
            return DataFlowStatus.UNHEALTHY, "Toolkit health check failed"
        except Exception as e:
            return DataFlowStatus.ERROR, f"Error checking toolkit status: {str(e)}" 