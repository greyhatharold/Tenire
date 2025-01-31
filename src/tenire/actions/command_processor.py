"""
Command processing module for the Tenire framework.

This module handles the interpretation and routing of commands from ChatGPT
to appropriate framework components. It acts as a mediator between the AI
directives and the framework's functionality.

Example command formats:
    Simple text: "PLACE_BET 100 on Mines"
    JSON: {"action": "PLACE_BET", "amount": 100, "game": "Mines", "params": {...}}
"""

# Standard library imports
import json
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict, Optional, ClassVar
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import threading
from concurrent.futures import ThreadPoolExecutor

from tenire.utils.logger import get_logger
from tenire.core.codex import Signal, SignalType
from tenire.servicers import get_signal_manager
from tenire.organizers.compactor import Compactor
from tenire.actions.constants import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_INIT_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_DELAY,
    DEFAULT_MAX_QUEUE_SIZE
)

# Configure logging
logger = get_logger(__name__)

@dataclass
class ProcessorConfig:
    """Configuration holder for command processor."""
    command_timeout: int = DEFAULT_COMMAND_TIMEOUT
    init_timeout: int = DEFAULT_INIT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_delay: float = DEFAULT_RETRY_DELAY
    max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE

    @classmethod
    def load_from_config(cls):
        """Load configuration values from config manager."""
        try:
            from tenire.core.config import config_manager
            if not config_manager._initialized:
                config_manager.initialize()
            
            config = config_manager.get_config()
            if config:
                return cls(
                    command_timeout=config.request_timeout,
                    init_timeout=config.request_timeout * 2,
                    max_retries=getattr(config, 'max_retries', DEFAULT_MAX_RETRIES),
                    retry_delay=getattr(config, 'retry_delay', DEFAULT_RETRY_DELAY),
                    max_queue_size=getattr(config, 'queue_size', DEFAULT_MAX_QUEUE_SIZE)
                )
            return cls()
        except Exception as e:
            logger.warning(f"Failed to load command processor config, using defaults: {str(e)}")
            return cls()

# Create configuration instance
processor_config = ProcessorConfig.load_from_config()

# Export for module access
__all__ = [
    'CommandProcessor',
    'CommandType',
    'ParsedCommand',
    'CommandParseError',
    'ProcessorConfig',
    'processor_config'
]

class CommandType(Enum):
    """Enumeration of supported command types."""
    PLACE_BET = auto()
    CHECK_BALANCE = auto()
    LOGIN = auto()
    LOGOUT = auto()
    NAVIGATE = auto()
    GUI_UPDATE = auto()  # New command type for GUI updates
    GUI_ACTION = auto()  # New command type for GUI-triggered actions
    UNKNOWN = auto()

    @classmethod
    def from_string(cls, command_str: str) -> 'CommandType':
        """Convert string to CommandType, defaulting to UNKNOWN if not recognized."""
        try:
            return cls[command_str.upper()]
        except KeyError:
            return cls.UNKNOWN


@dataclass
class ParsedCommand:
    """Data structure for holding parsed command information."""
    command_type: CommandType
    parameters: Dict[str, Any]
    raw_command: str


class CommandParseError(Exception):
    """Raised when command parsing fails."""
    pass


class CommandProcessor:
    """
    Processes and routes commands from ChatGPT to framework components.
    
    This class is responsible for:
    1. Parsing incoming command strings (text or JSON)
    2. Validating command structure and parameters
    3. Routing commands to appropriate handlers
    4. Providing a clean interface for adding new command types
    
    The class follows the Single Responsibility Principle by focusing solely
    on command interpretation and routing, delegating actual execution to
    other components.
    """

    _instance: ClassVar[Optional['CommandProcessor']] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()
    _executor: ClassVar[ThreadPoolExecutor] = ThreadPoolExecutor(max_workers=1)
    
    def __new__(cls):
        """Ensure singleton pattern with thread safety."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize the command processor with non-blocking initialization."""
        if hasattr(self, '_initialized'):
            return
            
        self._initialized = True
        self._command_handlers = {
            CommandType.PLACE_BET: self._handle_place_bet,
            CommandType.CHECK_BALANCE: self._handle_check_balance,
            CommandType.LOGIN: self._handle_login,
            CommandType.LOGOUT: self._handle_logout,
            CommandType.NAVIGATE: self._handle_navigate,
            CommandType.GUI_UPDATE: self._handle_gui_update,
            CommandType.GUI_ACTION: self._handle_gui_action,
        }
        self._signal_handlers = {}
        self._command_queue = asyncio.Queue(maxsize=processor_config.max_queue_size)
        self._processing_lock = asyncio.Lock()
        self._last_command_time = {}  # Track command timing
        self._startup_time = datetime.now()
        self._shutdown_requested = False
        self._initialization_lock = threading.Lock()
        self._initialization_future = None
        self._init_complete = asyncio.Event()
        
        # Start background initialization
        self._start_background_init()
        
        logger.info("Command processor initialized with default configuration")

    def _start_background_init(self) -> None:
        """Start background initialization in a separate thread."""
        try:
            with self._initialization_lock:
                if self._initialization_future is None:
                    def run_init():
                        try:
                            # Get existing event loop or create new one
                            try:
                                loop = asyncio.get_event_loop()
                            except RuntimeError:
                                loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(loop)
                            
                            # Run initialization in this loop
                            if not loop.is_closed():
                                if loop.is_running():
                                    future = asyncio.run_coroutine_threadsafe(
                                        self._initialize_processor(),
                                        loop
                                    )
                                    future.result()
                                else:
                                    loop.run_until_complete(self._initialize_processor())
                        except Exception as e:
                            logger.error(f"Background initialization failed: {str(e)}")
                        finally:
                            if not loop.is_running():
                                loop.close()
                    
                    # Submit to executor without blocking
                    self._initialization_future = self._executor.submit(run_init)
        except Exception as e:
            logger.error(f"Failed to start background initialization: {str(e)}")

    async def _initialize_processor(self):
        """Initialize processor components with timeout protection."""
        try:
            async with asyncio.timeout(processor_config.init_timeout):
                # Load configuration first
                try:
                    from tenire.core.config import config_manager
                    if not config_manager._initialized:
                        config_manager.initialize()
                    
                    # Update timeouts and limits from config if available
                    config = config_manager.get_config()
                    if config:
                        global DEFAULT_COMMAND_TIMEOUT, DEFAULT_INIT_TIMEOUT, DEFAULT_MAX_RETRIES, DEFAULT_RETRY_DELAY, DEFAULT_MAX_QUEUE_SIZE
                        DEFAULT_COMMAND_TIMEOUT = config.request_timeout
                        DEFAULT_INIT_TIMEOUT = config.request_timeout * 2
                        DEFAULT_MAX_RETRIES = getattr(config, 'max_retries', DEFAULT_MAX_RETRIES)
                        DEFAULT_RETRY_DELAY = getattr(config, 'retry_delay', DEFAULT_RETRY_DELAY)
                        DEFAULT_MAX_QUEUE_SIZE = getattr(config, 'queue_size', DEFAULT_MAX_QUEUE_SIZE)
                        
                        logger.info("Command processor configuration loaded successfully")
                except Exception as e:
                    logger.warning(f"Failed to load command processor config, using defaults: {str(e)}")

                # Wait for signal manager to be ready
                signal_manager = None
                for attempt in range(DEFAULT_MAX_RETRIES * 2):  # Double retries for critical initialization
                    try:
                        signal_manager = await get_signal_manager()
                        if signal_manager:
                            # Ensure signal manager is initialized
                            if not hasattr(signal_manager, '_processing_task') or not signal_manager._processing_task:
                                await signal_manager.initialize()
                            break
                    except Exception as e:
                        if attempt == (DEFAULT_MAX_RETRIES * 2) - 1:
                            logger.error(f"Failed to get signal manager: {str(e)}")
                            raise
                        logger.warning(f"Attempt {attempt + 1} to get signal manager failed, retrying: {str(e)}")
                        await asyncio.sleep(DEFAULT_RETRY_DELAY)

                if not signal_manager:
                    raise RuntimeError("Failed to initialize signal manager")

                # Register signal handlers with retry
                for attempt in range(DEFAULT_MAX_RETRIES):
                    try:
                        # Force reload of SignalType to ensure we have latest version
                        import importlib
                        from tenire.core import codex
                        importlib.reload(codex)
                        from tenire.core.codex import SignalType
                        
                        # Register handlers with proper async detection
                        signal_manager.register_handler(
                            SignalType.COMMAND_CONFIRMATION,
                            self._handle_command_confirmation,
                            priority=10,
                            is_async=True
                        )
                        signal_manager.register_handler(
                            SignalType.GUI_ACTION,
                            self._handle_gui_action_signal,
                            priority=5,
                            is_gui=True,
                            is_async=True
                        )
                        signal_manager.register_handler(
                            SignalType.GUI_UPDATE,
                            self._handle_gui_update_signal,
                            priority=5,
                            is_gui=True,
                            is_async=True
                        )
                        break
                    except Exception as e:
                        if attempt == DEFAULT_MAX_RETRIES - 1:
                            logger.error(f"Failed to register signal handlers: {str(e)}")
                            raise
                        logger.warning(f"Attempt {attempt + 1} failed, retrying: {str(e)}")
                        await asyncio.sleep(DEFAULT_RETRY_DELAY)
                
                # Register cleanup with compactor
                try:
                    compactor = Compactor()
                    compactor.register_cleanup_task(
                        name="command_processor_cleanup",
                        cleanup_func=self._cleanup,
                        priority=95,
                        is_async=True
                    )
                except Exception as e:
                    logger.error(f"Failed to register cleanup task: {str(e)}")
                
                # Start command processor task in the current event loop
                asyncio.create_task(self._process_command_queue())
                
                # Mark as initialized
                self._init_complete.set()
                logger.info("Command processor initialization completed")
                
        except asyncio.TimeoutError:
            logger.error(f"Command processor initialization timed out after {processor_config.init_timeout}s")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize command processor: {str(e)}")
            raise

    async def _process_command_queue(self):
        """Process commands from the queue with enhanced error handling."""
        while not self._shutdown_requested:
            try:
                command = await self._command_queue.get()
                if command is None:  # Shutdown signal
                    break
                    
                cmd_type, cmd_data = command
                if cmd_type == "command":
                    try:
                        result = await self.process_command(cmd_data)
                        if result.get("success", False):
                            logger.debug(f"Successfully processed command: {cmd_data}")
                            
                            # Emit success signal
                            signal_manager = await get_signal_manager()
                            if signal_manager:
                                await signal_manager.emit(Signal(
                                    type=SignalType.COMMAND_COMPLETED,
                                    data={
                                        "command": cmd_data,
                                        "result": result,
                                        "success": True
                                    },
                                    source="command_processor"
                                ))
                        else:
                            logger.warning(f"Command processing failed: {result.get('error')}")
                            
                            # Emit error signal
                            signal_manager = await get_signal_manager()
                            if signal_manager:
                                await signal_manager.emit(Signal(
                                    type=SignalType.COMMAND_ERROR,
                                    data={
                                        "command": cmd_data,
                                        "error": result.get("error"),
                                        "result": result
                                    },
                                    source="command_processor"
                                ))
                    except Exception as e:
                        logger.error(f"Error processing command {cmd_data}: {str(e)}")
                        
                        # Emit error signal
                        signal_manager = await get_signal_manager()
                        if signal_manager:
                            await signal_manager.emit(Signal(
                                type=SignalType.COMMAND_ERROR,
                                data={
                                    "command": cmd_data,
                                    "error": str(e)
                                },
                                source="command_processor"
                            ))
                    finally:
                        self._command_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in command queue processing: {str(e)}")
                await asyncio.sleep(DEFAULT_RETRY_DELAY)

    @asynccontextmanager
    async def _command_timeout(self, command_type: CommandType):
        """Context manager for command timeout handling."""
        try:
            async with asyncio.timeout(processor_config.command_timeout):
                yield
        except asyncio.TimeoutError:
            logger.error(f"Command {command_type} timed out after {processor_config.command_timeout}s")
            self._last_command_time[command_type] = datetime.now()
            raise

    async def process_command(self, command_text: str) -> Dict[str, Any]:
        """Process a command with non-blocking initialization check."""
        logger.info("Processing command: %s", command_text)
        
        # Check initialization status without blocking
        if not self._init_complete.is_set():
            return {
                "success": False,
                "error": "Command processor is still initializing",
                "display": {
                    "text": "System is starting up, please wait",
                    "style": "warning"
                }
            }

        try:
            # Emit command received signal
            signal_manager = await get_signal_manager()
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.COMMAND_RECEIVED,
                    data={"command": command_text},
                    source="command_processor"
                ))

            if self._command_queue.full():
                return {
                    "success": False,
                    "error": "Command queue is full, please try again later",
                    "display": {
                        "text": "System is busy, please wait",
                        "style": "warning"
                    }
                }

            # Parse command type and parameters
            command_type = self._parse_command_type(command_text)
            params = self._parse_text_parameters(command_text)
            
            # Check if we should throttle this command type
            last_time = self._last_command_time.get(command_type)
            if last_time and (datetime.now() - last_time) < timedelta(seconds=DEFAULT_RETRY_DELAY):
                return {
                    "success": False,
                    "error": f"Please wait before retrying {command_type.name}",
                    "display": {
                        "text": "Command throttled, please wait",
                        "style": "warning"
                    }
                }

            # Add command type to parameters for handlers
            params["command_type"] = command_type
            
            # Process command with timeout and retry
            for attempt in range(DEFAULT_MAX_RETRIES):
                try:
                    async with self._processing_lock:
                        async with self._command_timeout(command_type):
                            # Emit command processing signal
                            if signal_manager:
                                await signal_manager.emit(Signal(
                                    type=SignalType.COMMAND_PROCESSED,
                                    data={
                                        "command": command_text,
                                        "command_type": command_type.name,
                                        "params": params
                                    },
                                    source="command_processor"
                                ))
                            
                            # Handle different command types
                            result = None
                            if command_type == CommandType.PLACE_BET:
                                result = await self._handle_place_bet(params)
                            elif command_type == CommandType.CHECK_BALANCE:
                                result = await self._handle_check_balance(params)
                            elif command_type == CommandType.GUI_UPDATE:
                                result = await self._handle_gui_update(params)
                            elif command_type == CommandType.GUI_ACTION:
                                result = await self._handle_gui_action(params)
                            else:
                                result = {
                                    "success": True,
                                    "message": command_text,
                                    "type": "info",
                                    "display": {
                                        "text": f"Processing command: {command_text}",
                                        "style": "info"
                                    }
                                }
                            
                            # Update last command time
                            self._last_command_time[command_type] = datetime.now()
                            
                            # Ensure result has display information
                            if "display" not in result:
                                result["display"] = {
                                    "text": result.get("message", "Command processed"),
                                    "style": "success" if result.get("success", False) else "error"
                                }
                            
                            # Emit command completed signal
                            if signal_manager:
                                await signal_manager.emit(Signal(
                                    type=SignalType.COMMAND_COMPLETED,
                                    data={
                                        "command": command_text,
                                        "result": result,
                                        "success": result.get("success", False)
                                    },
                                    source="command_processor"
                                ))
                            
                            # Emit GUI update
                            await self._emit_gui_update(result)
                            return result
                            
                except asyncio.TimeoutError:
                    if attempt < DEFAULT_MAX_RETRIES - 1:
                        await asyncio.sleep(DEFAULT_RETRY_DELAY)
                        continue
                    error_result = {
                        "success": False,
                        "error": f"Command timed out after {processor_config.command_timeout}s",
                        "display": {
                            "text": "Command timed out, please try again",
                            "style": "error"
                        }
                    }
                    if signal_manager:
                        await signal_manager.emit(Signal(
                            type=SignalType.COMMAND_ERROR,
                            data={
                                "command": command_text,
                                "error": "timeout",
                                "result": error_result
                            },
                            source="command_processor"
                        ))
                    return error_result
                except Exception as e:
                    if attempt < DEFAULT_MAX_RETRIES - 1:
                        await asyncio.sleep(DEFAULT_RETRY_DELAY)
                        continue
                    error_result = {
                        "success": False,
                        "error": f"Command processing failed: {str(e)}",
                        "display": {
                            "text": f"Error: {str(e)}",
                            "style": "error"
                        }
                    }
                    if signal_manager:
                        await signal_manager.emit(Signal(
                            type=SignalType.COMMAND_ERROR,
                            data={
                                "command": command_text,
                                "error": str(e),
                                "result": error_result
                            },
                            source="command_processor"
                        ))
                    await self._emit_gui_update(error_result)
                    logger.error(f"Command processing failed: {str(e)}")
                    return error_result
                    
        except Exception as e:
            error_result = {
                "success": False,
                "error": f"Command processing failed: {str(e)}",
                "display": {
                    "text": f"Error: {str(e)}",
                    "style": "error"
                }
            }
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.COMMAND_ERROR,
                    data={
                        "command": command_text,
                        "error": str(e),
                        "result": error_result
                    },
                    source="command_processor"
                ))
            await self._emit_gui_update(error_result)
            logger.error(f"Command processing failed: {str(e)}")
            return error_result

    def _parse_command(self, command: str) -> ParsedCommand:
        """
        Parse a command string into structured format.
        
        Attempts to parse as JSON first, falls back to text parsing.
        
        Args:
            command: Raw command string
            
        Returns:
            ParsedCommand object containing structured command data
            
        Raises:
            CommandParseError: If parsing fails
        """
        # Try JSON parsing first
        try:
            data = json.loads(command)
            if isinstance(data, dict) and "action" in data:
                command_type = CommandType.from_string(data["action"])
                # Remove action from parameters
                params = {k: v for k, v in data.items() if k != "action"}
                return ParsedCommand(command_type, params, command)
        except json.JSONDecodeError:
            pass

        # Fall back to text parsing
        try:
            parts = command.strip().split(maxsplit=1)
            if not parts:
                raise CommandParseError("Empty command")
            
            command_type = CommandType.from_string(parts[0])
            params = self._parse_text_parameters(parts[1] if len(parts) > 1 else "")
            
            # Special handling for betting commands
            if command_type == CommandType.PLACE_BET:
                params = self._enhance_bet_parameters(params)
            
            return ParsedCommand(command_type, params, command)
            
        except Exception as e:
            raise CommandParseError(f"Failed to parse command: {str(e)}")

    def _parse_text_parameters(self, param_string: str) -> Dict[str, Any]:
        """
        Parse space-separated parameters into a dictionary.
        
        Example:
            "100 on Mines with config=fast" ->
            {"amount": "100", "game": "Mines", "config": "fast"}
        """
        params = {}
        if not param_string:
            return params

        # Split on spaces, but handle quoted strings
        parts = param_string.split()
        current_key = None
        
        for part in parts:
            if "=" in part:
                key, value = part.split("=", 1)
                params[key.lower()] = value
            elif part.lower() == "on" and parts.index(part) < len(parts) - 1:
                # Handle "on Game" pattern
                params["game"] = parts[parts.index(part) + 1]
            elif not current_key and part.isdigit():
                # Assume first number is amount
                params["amount"] = float(part)
            
        return params

    def _enhance_bet_parameters(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enhance betting parameters with additional context and validation.
        Now supports GUI-specific parameters.
        """
        enhanced = super()._enhance_bet_parameters(params)
        
        # Handle GUI-specific parameters
        if "gui_source" in params:
            enhanced["gui_source"] = params["gui_source"]
            
        if "strategy_id" in params:
            enhanced["strategy_id"] = params["strategy_id"]
            
        return enhanced

    def _handle_place_bet(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle bet placement command with enhanced validation."""
        logger.info("Bet placement requested with params: %s", params)
        
        try:
            # Validate required parameters
            if "game" not in params:
                return {
                    "success": False,
                    "error": "Game type must be specified"
                }
            
            # Enhance parameters with defaults and validation
            enhanced_params = self._enhance_bet_parameters(params)
            
            return {
                "success": True,
                "bet": enhanced_params,
                "message": f"Bet placement prepared for {enhanced_params['game']}"
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to prepare bet: {str(e)}"
            }

    def _handle_check_balance(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle balance check command - to be implemented by account module."""
        logger.info("Balance check requested")
        return {"success": True, "message": "Balance check not yet implemented"}

    def _handle_login(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle login command - to be implemented with browser integration."""
        logger.info("Login requested")
        return {"success": True, "message": "Login not yet implemented"}

    def _handle_logout(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle logout command - to be implemented with browser integration."""
        logger.info("Logout requested")
        return {"success": True, "message": "Logout not yet implemented"}

    def _handle_navigate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle navigation command - to be implemented with browser integration."""
        logger.info("Navigation requested to: %s", params.get("url", "unspecified"))
        return {"success": True, "message": "Navigation not yet implemented"}

    def register_command_handler(self, command_type: CommandType, 
                               handler: callable) -> None:
        """
        Register a new command handler.
        
        This method allows for extensibility by registering new command handlers
        without modifying the core class.
        
        Args:
            command_type: The type of command to handle
            handler: Callable that takes parameters dict and returns result dict
        """
        self._command_handlers[command_type] = handler 

    def register_signal_handler(self, signal_type: str, handler: callable) -> None:
        """Register a handler for GUI signal updates."""
        self._signal_handlers[signal_type] = handler

    async def _handle_gui_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle GUI update commands by emitting appropriate signals."""
        try:
            signal_type = params.get("signal_type")
            if not signal_type:
                return {"success": False, "error": "No signal type specified"}

            handler = self._signal_handlers.get(signal_type)
            if handler:
                await handler(params)
                return {"success": True, "message": f"GUI update processed: {signal_type}"}
            else:
                return {"success": False, "error": f"No handler for signal type: {signal_type}"}
        except Exception as e:
            logger.error(f"GUI update failed: {str(e)}")
            return {"success": False, "error": str(e)}

    async def _handle_gui_action(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle actions triggered from the GUI."""
        try:
            action_type = params.get("action_type")
            if not action_type:
                return {"success": False, "error": "No action type specified"}

            # Convert GUI action to appropriate command type
            if action_type == "bet":
                return await self._handle_place_bet(params)
            elif action_type == "balance":
                return await self._handle_check_balance(params)
            else:
                return {"success": False, "error": f"Unsupported GUI action: {action_type}"}
        except Exception as e:
            logger.error(f"GUI action failed: {str(e)}")
            return {"success": False, "error": str(e)}

    def _parse_command_type(self, command: str) -> CommandType:
        """
        Parse the command type from a command string.
        
        This method handles multiple command formats:
        1. JSON format: {"action": "PLACE_BET", ...}
        2. Markdown format: **PLACE_BET** ...
        3. Plain text format: "PLACE_BET 100 on Mines"
        
        Args:
            command: The command string to parse
            
        Returns:
            CommandType: The parsed command type, defaults to UNKNOWN if not recognized
            
        Example:
            >>> _parse_command_type('{"action": "PLACE_BET", "amount": 100}')
            CommandType.PLACE_BET
            >>> _parse_command_type('**CHECK_BALANCE**')
            CommandType.CHECK_BALANCE
            >>> _parse_command_type('NAVIGATE to Mines')
            CommandType.NAVIGATE
        """
        if not command:
            return CommandType.UNKNOWN
            
        try:
            # Try JSON parsing first
            try:
                data = json.loads(command)
                if isinstance(data, dict) and "action" in data:
                    return CommandType.from_string(data["action"])
            except json.JSONDecodeError:
                pass
                
            # Handle markdown-formatted commands
            if "**" in command:
                parts = command.split("**")
                if len(parts) >= 2:
                    return CommandType.from_string(parts[1].strip())
                    
            # Handle plain text commands
            first_word = command.split()[0].strip().upper()
            
            # Direct command type mapping
            try:
                return CommandType[first_word]
            except KeyError:
                # Handle common command variations
                command_map = {
                    "GO": CommandType.NAVIGATE,
                    "BET": CommandType.PLACE_BET,
                    "CHECK": CommandType.CHECK_BALANCE,
                    "UPDATE": CommandType.GUI_UPDATE,
                    "ACTION": CommandType.GUI_ACTION
                }
                return command_map.get(first_word, CommandType.UNKNOWN)
                
        except Exception as e:
            logger.debug(f"Error parsing command type: {str(e)}")
            return CommandType.UNKNOWN

    async def _process_command_type(
        self, 
        command_type: str, 
        command: str
    ) -> Dict[str, Any]:
        """
        Process a command based on its type.
        
        Args:
            command_type: The type of command to process
            command: The full command string
            
        Returns:
            Dict containing success status and optional message/error
        """
        try:
            # Handle different command types
            if command_type == "text":
                # Process as text command
                return {
                    "success": True,
                    "message": command
                }
                
            # Add more command type handlers as needed
            return {
                "success": True,
                "message": f"Processed {command_type} command: {command}"
            }
            
        except Exception as e:
            logger.error(f"Error processing command type {command_type}: {str(e)}")
            return {
                "success": False,
                "error": f"Command processing failed: {str(e)}"
            }

    async def _emit_gui_update(self, result: Dict[str, Any]) -> None:
        """Emit a GUI update with retry on failure."""
        for attempt in range(DEFAULT_MAX_RETRIES):
            try:
                signal_manager = await get_signal_manager()
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.GUI_UPDATE,
                        data=result,
                        source="command_processor"
                    ))
                    return
            except Exception as e:
                if attempt < DEFAULT_MAX_RETRIES - 1:
                    await asyncio.sleep(DEFAULT_RETRY_DELAY)
                    continue
                logger.error(f"Failed to emit GUI update: {str(e)}")

    async def _handle_command_confirmation(self, signal: Signal) -> None:
        """Handle command confirmation signal."""
        try:
            command = signal.data.get("command")
            confirmed = signal.data.get("confirmed", False)
            completed = signal.data.get("completed", False)
            
            if not command:
                logger.warning("Received command confirmation signal without command")
                return
                
            logger.debug(f"Processing command confirmation: {command} (confirmed={confirmed})")
            
            if completed:
                if confirmed:
                    logger.info(f"Command confirmed and completed: {command}")
                    # Emit success signal
                    signal_manager = await get_signal_manager()
                    if signal_manager:
                        await signal_manager.emit(Signal(
                            type=SignalType.COMMAND_CONFIRMATION_RESPONSE,
                            data={
                                "command": command,
                                "confirmed": True,
                                "completed": True
                            },
                            source="command_processor"
                        ))
                else:
                    logger.info(f"Command cancelled: {command}")
                    # Emit cancellation signal
                    signal_manager = await get_signal_manager()
                    if signal_manager:
                        await signal_manager.emit(Signal(
                            type=SignalType.COMMAND_CANCELLED,
                            data={
                                "command": command,
                                "reason": "user_cancelled"
                            },
                            source="command_processor"
                        ))
            else:
                # Command is awaiting confirmation
                logger.debug(f"Command awaiting confirmation: {command}")
                # Request confirmation
                signal_manager = await get_signal_manager()
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.COMMAND_CONFIRMATION_REQUESTED,
                        data={
                            "command": command,
                            "requires_confirmation": True
                        },
                        source="command_processor"
                    ))
                
        except Exception as e:
            logger.error(f"Error handling command confirmation: {str(e)}")
            # Emit error signal
            try:
                signal_manager = await get_signal_manager()
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.COMMAND_ERROR,
                        data={
                            "command": command if command else "unknown",
                            "error": str(e)
                        },
                        source="command_processor"
                    ))
            except Exception as emit_error:
                logger.error(f"Failed to emit error signal: {str(emit_error)}")

    async def _handle_gui_action_signal(self, signal: Signal) -> None:
        """Handle GUI action signal."""
        action_type = signal.data.get("action_type")
        if action_type:
            try:
                result = await self._handle_gui_action(signal.data)
                # Emit result back to GUI
                signal_manager = await get_signal_manager()
                if signal_manager:
                    await signal_manager.emit_gui_update(
                        "action_result",
                        {"result": result, "action_type": action_type},
                        priority=5
                    )
            except Exception as e:
                logger.error(f"Error handling GUI action: {str(e)}")
                signal_manager = await get_signal_manager()
                if signal_manager:
                    await signal_manager.emit_gui_update(
                        "action_error",
                        {"error": str(e), "action_type": action_type},
                        priority=5
                    )

    async def _handle_gui_update_signal(self, signal: Signal) -> None:
        """Handle GUI update signal."""
        update_type = signal.data.get("update_type")
        if update_type:
            try:
                result = await self._handle_gui_update(signal.data)
                if not result.get("success", False):
                    logger.error(f"GUI update failed: {result.get('error')}")
            except Exception as e:
                logger.error(f"Error handling GUI update: {str(e)}")

    async def _cleanup(self) -> None:
        """Clean up processor resources."""
        if self._shutdown_requested:
            return
            
        self._shutdown_requested = True
        try:
            # Cancel any pending tasks
            if hasattr(asyncio, 'current_task'):
                current = asyncio.current_task()
                for task in asyncio.all_tasks():
                    if task is not current:
                        task.cancel()
            
            # Clear queues
            while not self._command_queue.empty():
                try:
                    self._command_queue.get_nowait()
                    self._command_queue.task_done()
                except asyncio.QueueEmpty:
                    break
            
            # Shutdown executor
            self._executor.shutdown(wait=False)
            
            logger.info("Command processor cleanup completed")
        except Exception as e:
            logger.error(f"Error during command processor cleanup: {str(e)}")

    def __del__(self):
        """Handle cleanup during deletion."""
        try:
            if hasattr(self, '_shutdown_requested') and not self._shutdown_requested:
                self._executor.shutdown(wait=False)
        except Exception:
            pass  # Ignore cleanup errors during deletion

    async def _handle_browser_command_signal(self, signal: Signal) -> None:
        """Handle browser command signal."""
        command = signal.data.get("command")
        params = signal.data.get("params", {})
        if command:
            try:
                # Process browser command
                result = await self._handle_browser_command({"command": command, **params})
                # Emit result back to GUI
                signal_manager = await get_signal_manager()
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.BROWSER_COMMAND_COMPLETED,
                        data={
                            "command": command,
                            "result": result,
                            "success": result.get("success", False)
                        },
                        source="command_processor"
                    ))
            except Exception as e:
                logger.error(f"Error handling browser command: {str(e)}")
                signal_manager = await get_signal_manager()
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.BROWSER_ERROR,
                        data={
                            "command": command,
                            "error": str(e),
                            "source": "command"
                        },
                        source="command_processor"
                    ))

    async def _handle_browser_action_signal(self, signal: Signal) -> None:
        """Handle browser action signal."""
        action = signal.data.get("action")
        params = signal.data.get("params", {})
        if action:
            try:
                # Process browser action
                result = await self._handle_browser_action({"action": action, **params})
                # Emit result back to GUI
                signal_manager = await get_signal_manager()
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.BROWSER_ACTION_COMPLETED,
                        data={
                            "action": action,
                            "result": result,
                            "success": result.get("success", False)
                        },
                        source="command_processor"
                    ))
            except Exception as e:
                logger.error(f"Error handling browser action: {str(e)}")
                signal_manager = await get_signal_manager()
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.BROWSER_ERROR,
                        data={
                            "action": action,
                            "error": str(e),
                            "source": "action"
                        },
                        source="command_processor"
                    ))

    async def _handle_browser_command(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle browser command execution."""
        try:
            command = params.get("command")
            if not command:
                return {"success": False, "error": "No command specified"}

            # Process browser command
            # This is a placeholder - actual implementation will depend on browser integration
            return {
                "success": True,
                "message": f"Browser command executed: {command}",
                "command": command,
                "params": params
            }
        except Exception as e:
            logger.error(f"Browser command failed: {str(e)}")
            return {"success": False, "error": str(e)}

    async def _handle_browser_action(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle browser action execution."""
        try:
            action = params.get("action")
            if not action:
                return {"success": False, "error": "No action specified"}

            # Process browser action
            # This is a placeholder - actual implementation will depend on browser integration
            return {
                "success": True,
                "message": f"Browser action executed: {action}",
                "action": action,
                "params": params
            }
        except Exception as e:
            logger.error(f"Browser action failed: {str(e)}")
            return {"success": False, "error": str(e)}

# Global instance - initialization is deferred
command_processor = CommandProcessor() 