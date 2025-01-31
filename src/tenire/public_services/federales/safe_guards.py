"""
Safeguards module for the Tenire framework.

This module implements various safety checks and validations to ensure secure
and responsible operation of the framework, particularly around sensitive
operations like betting and authentication.

Following Single Responsibility Principle, this module focuses solely on
implementing safety checks and validation logic.
"""

# Standard library imports
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Dict, Optional, Any, ClassVar
import asyncio
from contextlib import asynccontextmanager
import threading
from concurrent.futures import ThreadPoolExecutor

# Local imports
from tenire.core.config import config_manager
from tenire.servicers import SignalManager, get_signal_manager
from tenire.core.codex import Signal, SignalType
from tenire.utils.logger import get_logger
from tenire.organizers.compactor import Compactor

logger = get_logger(__name__)

# Safeguard timeouts and limits
SAFEGUARD_TIMEOUT = 10.0  # 10 seconds max for safeguard checks
MAX_RETRIES = 3  # Increased retries
RETRY_DELAY = 0.5  # Reduced delay between retries
COLD_CONDITION_MULTIPLIER = 1.5
INIT_TIMEOUT = 30.0
STARTUP_GRACE_PERIOD = 5.0

# Default betting values
DEFAULT_BET_AMOUNT = 1.0
DEFAULT_COOLDOWN_MINUTES = 1
DEFAULT_SESSION_LIMIT = 500.0

class SafeguardType(Enum):
    """Types of safeguards that can be applied."""
    FIRST_BET = auto()
    BET_AMOUNT = auto()
    BET_FREQUENCY = auto()
    PASSWORD_ENTRY = auto()
    WITHDRAWAL = auto()
    SESSION_DURATION = auto()
    LOSS_LIMIT = auto()

@dataclass
class SafeguardConfig:
    """Configuration for a specific safeguard."""
    enabled: bool = True
    requires_confirmation: bool = True
    cooldown_minutes: Optional[int] = None
    threshold_value: Optional[float] = None
    max_attempts: Optional[int] = None
    cold_condition_timeout: Optional[float] = None  # Additional timeout for cold conditions

class SafeguardManager:
    """
    Manages safety checks and validations for sensitive operations.
    
    This class is responsible for:
    1. Validating sensitive operations before execution
    2. Tracking betting patterns and frequency
    3. Managing confirmation requirements
    4. Enforcing cooldown periods
    5. Monitoring session duration
    """
    
    _instance: ClassVar[Optional['SafeguardManager']] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()
    _executor: ClassVar[ThreadPoolExecutor] = ThreadPoolExecutor(max_workers=2)  # Increased workers
    
    # Default values when config is not available
    DEFAULT_BET_AMOUNT: ClassVar[float] = DEFAULT_BET_AMOUNT
    DEFAULT_COOLDOWN_MINUTES: ClassVar[int] = DEFAULT_COOLDOWN_MINUTES
    DEFAULT_SESSION_LIMIT: ClassVar[float] = DEFAULT_SESSION_LIMIT
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize safeguard manager with non-blocking initialization."""
        if hasattr(self, '_initialized'):
            return
            
        # Mark as initialized to prevent re-initialization
        self._initialized = True
        self._startup_time = datetime.now()
        self._init_complete = asyncio.Event()
        self._shutdown_requested = False
        self._initialization_lock = threading.Lock()
        self._initialization_future = None
        self._check_lock = asyncio.Lock()
        
        # Initialize with default values in the current thread
        self._initialize_defaults()
        
        # Start background initialization
        self._start_background_init()
        
        logger.info("SafeguardManager initialized with default configurations")

    def _initialize_defaults(self) -> None:
        """Initialize default safeguard configurations and state tracking.
        
        This method sets up the initial state of the safeguard manager with default
        configurations for each safeguard type and initializes state tracking variables.
        """
        # Initialize safeguard configurations
        self._safeguard_configs = {
            SafeguardType.FIRST_BET: SafeguardConfig(
                requires_confirmation=True,
                cooldown_minutes=None,
                threshold_value=None,
                max_attempts=None
            ),
            SafeguardType.BET_AMOUNT: SafeguardConfig(
                requires_confirmation=True,
                cooldown_minutes=None,
                threshold_value=DEFAULT_BET_AMOUNT * 2,
                max_attempts=3
            ),
            SafeguardType.BET_FREQUENCY: SafeguardConfig(
                requires_confirmation=False,
                cooldown_minutes=DEFAULT_COOLDOWN_MINUTES,
                threshold_value=None,
                max_attempts=None
            ),
            SafeguardType.PASSWORD_ENTRY: SafeguardConfig(
                requires_confirmation=True,
                cooldown_minutes=5,
                threshold_value=None,
                max_attempts=3
            ),
            SafeguardType.WITHDRAWAL: SafeguardConfig(
                requires_confirmation=True,
                cooldown_minutes=None,
                threshold_value=None,
                max_attempts=None
            ),
            SafeguardType.SESSION_DURATION: SafeguardConfig(
                requires_confirmation=False,
                cooldown_minutes=None,
                threshold_value=120,  # 2 hours
                max_attempts=None
            ),
            SafeguardType.LOSS_LIMIT: SafeguardConfig(
                requires_confirmation=True,
                cooldown_minutes=None,
                threshold_value=DEFAULT_SESSION_LIMIT,
                max_attempts=None
            )
        }
        
        # Initialize state tracking
        self._last_actions: Dict[SafeguardType, datetime] = {}
        self._attempt_counts: Dict[SafeguardType, int] = {}
        self._session_start = datetime.now()
        self._total_losses = 0.0
        self._has_placed_first_bet = False
        self._is_cold_condition = False

    def _start_background_init(self) -> None:
        """Start background initialization in a separate thread."""
        try:
            with self._initialization_lock:
                if self._initialization_future is None:
                    def run_init():
                        try:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            try:
                                loop.run_until_complete(self._initialize_manager())
                            finally:
                                loop.close()
                        except Exception as e:
                            logger.error(f"Background initialization failed: {str(e)}")
                            self._is_cold_condition = True
                    
                    self._initialization_future = self._executor.submit(run_init)
        except Exception as e:
            logger.error(f"Failed to start background initialization: {str(e)}")
            self._is_cold_condition = True

    async def _initialize_manager(self):
        """Initialize manager components with improved timeout handling."""
        initialization_tasks = []
        try:
            # Create tasks for parallel initialization
            initialization_tasks = [
                asyncio.create_task(self._initialize_config()),
                asyncio.create_task(self._initialize_signal_handlers()),
                asyncio.create_task(self._initialize_cleanup_handlers())
            ]
            
            # Wait for all tasks with timeout
            async with asyncio.timeout(INIT_TIMEOUT):
                results = await asyncio.gather(*initialization_tasks, return_exceptions=True)
                
                # Check for any exceptions
                for result in results:
                    if isinstance(result, Exception):
                        logger.error(f"Initialization task failed: {str(result)}")
                        self._is_cold_condition = True
                
            # Mark as initialized even if some tasks failed
            self._init_complete.set()
            logger.info("SafeguardManager initialization completed")
                
        except asyncio.TimeoutError:
            logger.error(f"SafeguardManager initialization timed out after {INIT_TIMEOUT}s")
            self._is_cold_condition = True
            # Still mark as initialized to allow degraded operation
            self._init_complete.set()
        except Exception as e:
            logger.error(f"Failed to initialize SafeguardManager: {str(e)}")
            self._is_cold_condition = True
            self._init_complete.set()
        finally:
            # Cancel any remaining tasks
            for task in initialization_tasks:
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _initialize_signal_handlers(self):
        """Initialize signal handlers with retry."""
        for attempt in range(MAX_RETRIES):
            try:
                signal_manager = await get_signal_manager()
                if signal_manager:
                    signal_manager.register_handler(
                        SignalType.SAFEGUARD_CHECK,
                        self._handle_safeguard_signal,
                        priority=10
                    )
                    return
            except Exception as e:
                if attempt == MAX_RETRIES - 1:
                    logger.error(f"Failed to initialize signal handlers after {MAX_RETRIES} attempts: {str(e)}")
                    raise
                await asyncio.sleep(RETRY_DELAY)

    async def _initialize_config(self):
        """Initialize configuration with retry."""
        for attempt in range(MAX_RETRIES):
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(self._executor, self.update_from_config)
                return
            except Exception as e:
                if attempt == MAX_RETRIES - 1:
                    logger.error(f"Failed to initialize config after {MAX_RETRIES} attempts: {str(e)}")
                    raise
                await asyncio.sleep(RETRY_DELAY)

    async def _initialize_cleanup_handlers(self):
        """Initialize cleanup handlers with retry."""
        for attempt in range(MAX_RETRIES):
            try:
                compactor = Compactor()
                compactor.register_cleanup_task(
                    name="safeguard_manager_cleanup",
                    cleanup_func=self._cleanup,
                    priority=98,
                    is_async=True
                )
                return
            except Exception as e:
                if attempt == MAX_RETRIES - 1:
                    logger.error(f"Failed to initialize cleanup handlers after {MAX_RETRIES} attempts: {str(e)}")
                    raise
                await asyncio.sleep(RETRY_DELAY)

    def update_from_config(self) -> None:
        """Update safeguard settings from configuration."""
        try:
            betting_config = config_manager.get_betting_config()
            
            # Update bet amount safeguard
            self._safeguard_configs[SafeguardType.BET_AMOUNT].threshold_value = (
                betting_config.default_amount * 2
            )
            
            # Update bet frequency safeguard
            self._safeguard_configs[SafeguardType.BET_FREQUENCY].cooldown_minutes = (
                betting_config.cooldown_minutes
            )
            
            # Update loss limit safeguard
            self._safeguard_configs[SafeguardType.LOSS_LIMIT].threshold_value = (
                betting_config.session_limit
            )
            
            logger.info("SafeguardManager updated with configuration values")
            
        except Exception as e:
            logger.warning(f"Failed to update SafeguardManager from config: {str(e)}")
            # Continue with default values
    
    @asynccontextmanager
    async def _safeguard_timeout(self, safeguard_type: SafeguardType):
        """Context manager for safeguard timeout handling."""
        config = self._safeguard_configs[safeguard_type]
        timeout = config.cold_condition_timeout if self._is_cold_condition else SAFEGUARD_TIMEOUT
        
        try:
            async with asyncio.timeout(timeout):
                yield
        except asyncio.TimeoutError:
            logger.error(f"Safeguard {safeguard_type} check timed out after {timeout}s")
            raise

    async def check_safeguard(
        self,
        safeguard_type: SafeguardType,
        value: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Check if an action is allowed with improved initialization handling."""
        # Allow checks to proceed with defaults even if not fully initialized
        if not self._init_complete.is_set():
            logger.debug("Safeguard check before initialization complete, using conservative defaults")
            config = self._safeguard_configs.get(safeguard_type)
            if not config:
                return False
            return config.requires_confirmation

        config = self._safeguard_configs[safeguard_type]
        if not config.enabled:
            return True
            
        try:
            async with self._check_lock:  # Ensure sequential checks
                async with self._safeguard_timeout(safeguard_type):
                    # Check cooldown period
                    if config.cooldown_minutes:
                        last_action = self._last_actions.get(safeguard_type)
                        if last_action:
                            cooldown = timedelta(minutes=config.cooldown_minutes)
                            if datetime.now() - last_action < cooldown:
                                await self._emit_safeguard_signal(
                                    safeguard_type,
                                    False,
                                    "Action blocked by cooldown period"
                                )
                                return False
                    
                    # Check threshold value
                    if config.threshold_value is not None and value is not None:
                        if value > config.threshold_value:
                            await self._emit_safeguard_signal(
                                safeguard_type,
                                False,
                                f"Value {value} exceeds threshold {config.threshold_value}"
                            )
                            return False
                    
                    # Check attempt limits
                    if config.max_attempts:
                        attempts = self._attempt_counts.get(safeguard_type, 0)
                        if attempts >= config.max_attempts:
                            await self._emit_safeguard_signal(
                                safeguard_type,
                                False,
                                f"Maximum attempts ({config.max_attempts}) exceeded"
                            )
                            return False
                    
                    # Special handling for first bet
                    if safeguard_type == SafeguardType.FIRST_BET and not self._has_placed_first_bet:
                        self._has_placed_first_bet = True
                        await self._emit_safeguard_signal(
                            safeguard_type,
                            True,
                            "First bet requires confirmation"
                        )
                        return config.requires_confirmation
                    
                    # Update tracking with retry on failure
                    for attempt in range(MAX_RETRIES):
                        try:
                            self._last_actions[safeguard_type] = datetime.now()
                            self._attempt_counts[safeguard_type] = self._attempt_counts.get(safeguard_type, 0) + 1
                            break
                        except Exception as e:
                            if attempt == MAX_RETRIES - 1:
                                logger.error(f"Failed to update tracking after {MAX_RETRIES} attempts: {str(e)}")
                            else:
                                await asyncio.sleep(RETRY_DELAY)
                    
                    # Check if confirmation is required
                    if config.requires_confirmation:
                        await self._emit_safeguard_signal(
                            safeguard_type,
                            True,
                            "Action requires confirmation"
                        )
                        return True
                    
                    return True
            
        except asyncio.TimeoutError:
            logger.error(f"Safeguard check timed out for {safeguard_type}")
            self._is_cold_condition = True  # Mark as cold condition for future checks
            return False
        except Exception as e:
            logger.error(f"Error in safeguard check: {str(e)}")
            return False
    
    async def _emit_safeguard_signal(
        self,
        safeguard_type: SafeguardType,
        allowed: bool,
        message: str
    ) -> None:
        """Emit a signal for safeguard checks with retry on failure."""
        for attempt in range(MAX_RETRIES):
            try:
                await SignalManager().emit(Signal(
                    type=SignalType.SAFEGUARD_CHECK,
                    data={
                        "type": safeguard_type.name,
                        "allowed": allowed,
                        "message": message,
                        "requires_confirmation": self._safeguard_configs[safeguard_type].requires_confirmation,
                        "is_cold_condition": self._is_cold_condition
                    },
                    source="safeguard_manager"
                ))
                return
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                logger.error(f"Failed to emit safeguard signal: {str(e)}")

    def update_loss_tracking(self, amount: float) -> None:
        """Update the loss tracking amount with retry on failure."""
        for attempt in range(MAX_RETRIES):
            try:
                self._total_losses += amount
                return
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    asyncio.sleep(RETRY_DELAY)
                    continue
                logger.error(f"Failed to update loss tracking: {str(e)}")
        
    def reset_attempt_count(self, safeguard_type: SafeguardType) -> None:
        """Reset the attempt count for a specific safeguard with retry."""
        for attempt in range(MAX_RETRIES):
            try:
                self._attempt_counts[safeguard_type] = 0
                return
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    asyncio.sleep(RETRY_DELAY)
                    continue
                logger.error(f"Failed to reset attempt count: {str(e)}")
        
    def get_session_duration(self) -> int:
        """Get current session duration in minutes."""
        try:
            return int((datetime.now() - self._session_start).total_seconds() / 60)
        except Exception as e:
            logger.error(f"Error calculating session duration: {str(e)}")
            return 0

    async def _cleanup(self) -> None:
        """Clean up manager resources."""
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
            
            # Shutdown executor
            self._executor.shutdown(wait=False)
            
            logger.info("SafeguardManager cleanup completed")
        except Exception as e:
            logger.error(f"Error during SafeguardManager cleanup: {str(e)}")

    def __del__(self):
        """Handle cleanup during deletion."""
        try:
            if hasattr(self, '_shutdown_requested') and not self._shutdown_requested:
                self._executor.shutdown(wait=False)
        except Exception:
            pass  # Ignore cleanup errors during deletion

    async def _handle_safeguard_signal(self, signal: Signal) -> None:
        """Handle safeguard check signals."""
        try:
            safeguard_type = SafeguardType[signal.data.get("type")]
            value = signal.data.get("value")
            metadata = signal.data.get("metadata", {})
            
            # Perform safeguard check
            result = await self.check_safeguard(safeguard_type, value, metadata)
            
            # Emit result signal
            signal_manager = SignalManager()
            await signal_manager.emit(Signal(
                type=SignalType.SAFEGUARD_RESULT,
                data={
                    "type": safeguard_type.name,
                    "allowed": result,
                    "value": value,
                    "metadata": metadata
                },
                source="safeguard_manager"
            ))
        except Exception as e:
            logger.error(f"Error handling safeguard signal: {str(e)}")
            # Emit error signal
            signal_manager = SignalManager()
            await signal_manager.emit(Signal(
                type=SignalType.SAFEGUARD_ERROR,
                data={
                    "error": str(e),
                    "type": signal.data.get("type"),
                    "value": signal.data.get("value")
                },
                source="safeguard_manager"
            ))

# Global instance - initialization is deferred
safeguard_manager = SafeguardManager() 