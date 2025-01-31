"""
Toolkit manager for the Tenire framework.

This module manages the registration and access to various toolkits,
providing a central point for toolkit coordination.
"""

from typing import Dict, List, Optional, Type
import asyncio
from collections import defaultdict
from datetime import datetime

from tenire.toolkit.base import ToolkitInterface
from tenire.utils.logger import get_logger
from tenire.core.codex import Signal, SignalType, TimingConfig
from tenire.structure.component_types import ComponentType
from tenire.structure.component_registry import component_registry
from tenire.organizers.compactor import compactor
from tenire.core.container import container
from tenire.servicers import SignalManager
from tenire.core.codex import DataFlowPriority

logger = get_logger(__name__)

class ToolkitManager:
    """Manages registration and access to toolkits."""
    
    _instance = None
    _lock = asyncio.Lock()
    
    def __new__(cls):
        """Ensure singleton pattern with thread safety."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
        
    def __init__(self, signal_manager: Optional[SignalManager] = None):
        """Initialize the toolkit manager.
        
        Args:
            signal_manager: Optional custom signal manager instance
        """
        if not hasattr(self, '_initialized'):
            self._initialized = True
            self._signal_manager = signal_manager or SignalManager()
            self._toolkits: Dict[str, ToolkitInterface] = {}
            self._command_map: Dict[str, str] = {}  # command -> toolkit_name
            self._cleanup_tasks: List[asyncio.Task] = []
            self._health_checks = {
                "toolkit_health": self._check_toolkit_health,
                "command_health": self._check_command_health
            }
            self._timing_config = TimingConfig(
                min_interval=0.1,
                max_interval=1.0,
                burst_limit=25,
                cooldown=0.5,
                priority=85
            )
            
            # Register with component registry
            self._register_with_registry()
            
    async def _register_with_registry(self) -> None:
        """Register toolkit manager with component registry."""
        try:
            await component_registry.register_component(
                name="toolkit_manager",
                component=self,
                component_type=ComponentType.CORE,
                provides=["toolkit_management", "command_routing"],
                dependencies={"signal_manager", "compactor"},
                priority=70,
                health_checks=self._health_checks,
                timing_config=self._timing_config,
                cleanup_priority=90,
                tags={"toolkit", "core", "management"},
                is_critical=True
            )
            
            # Register cleanup with compactor
            compactor.register_cleanup_task(
                name="toolkit_manager_cleanup",
                cleanup_func=self.cleanup,
                priority=90,
                is_async=True,
                metadata={"tags": ["toolkit", "cleanup"]}
            )
            
            logger.info("Registered toolkit manager with component registry")
            
        except Exception as e:
            logger.error(f"Error registering toolkit manager: {str(e)}")
            raise
            
    async def _check_toolkit_health(self) -> tuple[bool, str]:
        """Check health of registered toolkits."""
        try:
            unhealthy_toolkits = []
            for name, toolkit in self._toolkits.items():
                if hasattr(toolkit, 'check_health'):
                    is_healthy = await toolkit.check_health()
                    if not is_healthy:
                        unhealthy_toolkits.append(name)
                        
            if unhealthy_toolkits:
                return False, f"Unhealthy toolkits: {', '.join(unhealthy_toolkits)}"
            return True, "All toolkits healthy"
            
        except Exception as e:
            return False, f"Error checking toolkit health: {str(e)}"
            
    async def _check_command_health(self) -> tuple[bool, str]:
        """Check health of command routing."""
        try:
            if not self._command_map:
                return False, "No commands registered"
                
            # Check for command conflicts
            command_conflicts = defaultdict(list)
            for command, toolkit in self._command_map.items():
                command_conflicts[command].append(toolkit)
                
            conflicts = {cmd: toolkits for cmd, toolkits in command_conflicts.items() 
                       if len(toolkits) > 1}
                       
            if conflicts:
                return False, f"Command conflicts detected: {conflicts}"
            return True, "Command routing healthy"
            
        except Exception as e:
            return False, f"Error checking command health: {str(e)}"
            
    async def register_toolkit(self, toolkit: ToolkitInterface) -> None:
        """
        Register a new toolkit.
        
        Args:
            toolkit: The toolkit to register
        """
        try:
            name = toolkit.name
            if name in self._toolkits:
                raise ValueError(f"Toolkit {name} already registered")
                
            self._toolkits[name] = toolkit
            
            # Register commands
            for command in toolkit.supported_commands:
                if command in self._command_map:
                    logger.warning(
                        f"Command {command} already registered to {self._command_map[command]}, "
                        f"overriding with {name}"
                    )
                self._command_map[command] = name
                
            logger.info(f"Registered toolkit: {name}")
            
        except Exception as e:
            logger.error(f"Error registering toolkit: {str(e)}")
            raise
            
    async def get_toolkit(self, name: str) -> Optional[ToolkitInterface]:
        """
        Get a registered toolkit by name.
        
        Args:
            name: Name of the toolkit
            
        Returns:
            The toolkit if found, None otherwise
        """
        return self._toolkits.get(name)
        
    async def get_toolkit_for_command(self, command: str) -> Optional[ToolkitInterface]:
        """
        Get the toolkit that handles a specific command.
        
        Args:
            command: The command to look up
            
        Returns:
            The toolkit if found, None otherwise
        """
        toolkit_name = self._command_map.get(command)
        if toolkit_name:
            return self._toolkits.get(toolkit_name)
        return None
        
    async def execute_command(self, command: str, params: Dict[str, str]) -> Dict[str, str]:
        """
        Execute a command using the appropriate toolkit.
        
        Args:
            command: The command to execute
            params: Parameters for the command
            
        Returns:
            Dict containing the execution results
            
        Raises:
            ValueError: If command or parameters are invalid
            RuntimeError: If execution fails
        """
        try:
            # Emit command received signal
            await self._signal_manager.emit(Signal(
                type=SignalType.COMMAND_RECEIVED,
                data={
                    'command': command,
                    'params': params,
                    'timestamp': datetime.now().isoformat()
                },
                source='toolkit_manager',
                priority=DataFlowPriority.HIGH.value
            ))
            
            # Get toolkit
            toolkit = await self.get_toolkit_for_command(command)
            if not toolkit:
                error = f"No toolkit found for command: {command}"
                logger.error(error)
                await self._signal_manager.emit(Signal(
                    type=SignalType.COMMAND_ERROR,
                    data={
                        'command': command,
                        'error': error,
                        'timestamp': datetime.now().isoformat()
                    },
                    source='toolkit_manager',
                    priority=DataFlowPriority.HIGH.value
                ))
                return {
                    "success": False,
                    "error": error
                }
                
            # Validate command
            try:
                is_valid = await toolkit.validate_command(command, params)
            except Exception as e:
                error = f"Error validating command {command}: {str(e)}"
                logger.error(error)
                await self._signal_manager.emit(Signal(
                    type=SignalType.COMMAND_ERROR,
                    data={
                        'command': command,
                        'error': error,
                        'timestamp': datetime.now().isoformat()
                    },
                    source='toolkit_manager',
                    priority=DataFlowPriority.HIGH.value
                ))
                return {
                    "success": False,
                    "error": error
                }
                
            if not is_valid:
                error = f"Invalid command or parameters: {command}"
                logger.error(error)
                await self._signal_manager.emit(Signal(
                    type=SignalType.COMMAND_ERROR,
                    data={
                        'command': command,
                        'error': error,
                        'timestamp': datetime.now().isoformat()
                    },
                    source='toolkit_manager',
                    priority=DataFlowPriority.HIGH.value
                ))
                return {
                    "success": False,
                    "error": error
                }
                
            # Execute command
            try:
                result = await toolkit.execute_command(command, params)
            except Exception as e:
                error = f"Error executing command {command}: {str(e)}"
                logger.error(error)
                await self._signal_manager.emit(Signal(
                    type=SignalType.COMMAND_ERROR,
                    data={
                        'command': command,
                        'error': error,
                        'timestamp': datetime.now().isoformat()
                    },
                    source='toolkit_manager',
                    priority=DataFlowPriority.HIGH.value
                ))
                return {
                    "success": False,
                    "error": error
                }
                
            # Emit completion signal
            await self._signal_manager.emit(Signal(
                type=SignalType.COMMAND_COMPLETED,
                data={
                    'command': command,
                    'result': result,
                    'timestamp': datetime.now().isoformat()
                },
                source='toolkit_manager',
                priority=DataFlowPriority.HIGH.value
            ))
            
            return result
            
        except Exception as e:
            error = f"Unexpected error executing command {command}: {str(e)}"
            logger.error(error)
            await self._signal_manager.emit(Signal(
                type=SignalType.ERROR,
                data={
                    'error': error,
                    'context': 'command_execution',
                    'timestamp': datetime.now().isoformat()
                },
                source='toolkit_manager',
                priority=DataFlowPriority.HIGH.value
            ))
            return {
                "success": False,
                "error": error
            }
            
    async def cleanup(self) -> None:
        """Clean up all registered toolkits."""
        try:
            for name, toolkit in self._toolkits.items():
                try:
                    await toolkit.cleanup()
                except Exception as e:
                    logger.error(f"Error cleaning up toolkit {name}: {str(e)}")
                    
            self._toolkits.clear()
            self._command_map.clear()
            
            # Cancel any pending tasks
            for task in self._cleanup_tasks:
                if not task.done():
                    task.cancel()
                    
            logger.info("Cleaned up toolkit manager")
            
        except Exception as e:
            logger.error(f"Error during toolkit manager cleanup: {str(e)}")
            raise
            
    def __del__(self):
        """Ensure cleanup on deletion."""
        if hasattr(self, '_cleanup_tasks'):
            for task in self._cleanup_tasks:
                if not task.done():
                    task.cancel()

# Global instance
toolkit_manager = ToolkitManager() 

# Register with container
container.register_sync('toolkit_manager', toolkit_manager) 