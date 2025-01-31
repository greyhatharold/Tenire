"""
Modern sidebar GUI module for the Tenire framework using PySide6.

This module provides a sleek chat-like interface for displaying responses,
current actions, and allowing user prompts while integrating seamlessly
with the existing framework components.
"""

# Standard library imports
import sys
import queue
import logging
import asyncio
import multiprocessing
from typing import Optional, Callable, Dict, Any, List, Tuple, Awaitable
from datetime import datetime

# Third-party imports
import qasync
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout
)
from PySide6.QtCore import Signal, Slot, QTimer, QEventLoop

# Local imports
from tenire.utils.logger import get_logger
from tenire.servicers import SignalManager
from tenire.core.codex import Signal as CoreSignal, SignalType
from tenire.organizers.compactor import compactor
from tenire.organizers.scheduler import universal_timer
from tenire.gui.processes.gui_process import GUIProcess, AsyncSignalEmitter
from .widgets import (
    ChatWidget,
    InputWidget,
    CommandConfirmationWidget,
    SafeguardConfirmationWidget,
)
from .handlers.logging_handler import SidebarHandler
from tenire.core.event_loop import event_loop_manager

logger = get_logger(__name__)

class MainGUIWidget(QWidget):
    """Main widget containing all GUI components."""
    
    # Qt signals for thread-safe updates
    message_received = Signal(str, bool)
    status_updated = Signal(str)
    command_processed = Signal(dict)
    state_updated = Signal(dict)
    error_occurred = Signal(str)  # Signal for error handling
    cleanup_requested = Signal()  # Signal for cleanup coordination
    
    # Browser-related signals
    browser_command = Signal(str, dict)  # Signal for browser command execution
    browser_action = Signal(str, dict)  # Signal for browser action execution
    browser_state = Signal(dict)  # Signal for browser state updates
    browser_error = Signal(str, str)  # Signal for browser errors (error, context)
    
    def _update_state(self, **kwargs):
        """Update internal state and notify parent process if queues are initialized."""
        try:
            self._state.update(kwargs)
            if hasattr(self, 'response_queue') and self.response_queue:
                try:
                    self.response_queue.put(("state_update", self._state.copy()))
                except Exception as e:
                    logger.error(f"Error sending state update: {str(e)}")
                    self.error_occurred.emit(str(e))
            
            # Use event loop to emit state update
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(lambda: self.state_updated.emit(self._state.copy()))
            else:
                self.state_updated.emit(self._state.copy())
            
            # Notify concurrency manager if available
            try:
                from tenire.organizers.concurrency import concurrency_manager
                if concurrency_manager:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(concurrency_manager._update_gui_state(**kwargs))
                    else:
                        asyncio.run(concurrency_manager._update_gui_state(**kwargs))
            except Exception as e:
                logger.debug(f"Could not notify concurrency manager of state update: {str(e)}")
        except Exception as e:
            logger.error(f"Error in _update_state: {str(e)}")

    def _handle_signal_emit(self, signal_type: SignalType, data: dict) -> None:
        """Helper method to safely emit signals in Qt context."""
        try:
            loop = asyncio.get_event_loop()
            signal_manager = None
            
            async def emit_signal():
                nonlocal signal_manager
                try:
                    from tenire.servicers import get_signal_manager
                    signal_manager = await get_signal_manager()
                    if signal_manager:
                        await signal_manager.emit(CoreSignal(
                            type=signal_type,
                            data=data,
                            source="gui"
                        ))
                except Exception as e:
                    logger.error(f"Error emitting signal: {str(e)}")
            
            if loop.is_running():
                loop.create_task(emit_signal())
            else:
                asyncio.run(emit_signal())
                
        except Exception as e:
            logger.error(f"Error in _handle_signal_emit: {str(e)}")

    @Slot(str, bool)
    def _on_message_received(self, text: str, is_user: bool):
        """Handle received messages with proper signal emission."""
        self.add_message(text, is_user)
        self._handle_signal_emit(
            SignalType.GUI_MESSAGE_RECEIVED,
            {"text": text, "is_user": is_user}
        )

    @Slot(str)
    def _on_status_updated(self, status: str):
        """Handle status updates with proper signal emission."""
        self._handle_signal_emit(
            SignalType.GUI_STATUS_UPDATED,
            {"status": status}
        )
        self._run_coroutine(self.update_status(status))

    @Slot(dict)
    def _on_command_processed(self, result: dict):
        """Handle processed commands with proper signal emission."""
        self._handle_signal_emit(
            SignalType.COMMAND_PROCESSED,
            {"result": result}
        )
        if result.get("success"):
            self._run_coroutine(self.update_status("Command executed successfully"))
            if result.get("message"):
                self._run_coroutine(self.add_message_async(result["message"], is_user=False))
        else:
            error_msg = result.get("error", "Unknown error")
            self._run_coroutine(self.update_status(f"Command failed: {error_msg}"))
            self._run_coroutine(self.add_message_async(f"Error: {error_msg}", is_user=False))

    @Slot(dict)
    def _on_state_updated(self, state: dict):
        """Handle state updates with proper signal emission."""
        self._handle_signal_emit(
            SignalType.GUI_STATE,
            {"state": state}
        )
        self._run_coroutine(self.update_gui_state(state))

    @Slot(str)
    def _on_error_occurred(self, error_msg: str):
        """Handle error signal with proper signal emission."""
        self._handle_signal_emit(
            SignalType.GUI_ERROR,
            {"error": error_msg}
        )
        self.add_message(f"Error: {error_msg}", is_system=True)

    @Slot()
    def _on_cleanup_requested(self):
        """Handle cleanup request with proper signal emission."""
        self._handle_signal_emit(
            SignalType.GUI_CLEANUP_REQUESTED,
            {"status": "cleanup_requested"}
        )
        self._run_coroutine(self._cleanup_gui())

    def _run_coroutine(self, coro: Callable[..., Awaitable[Any]]) -> Optional[asyncio.Task]:
        """
        Safely run a coroutine in the Qt event loop context using qasync.
        
        Args:
            coro: The coroutine to run
            
        Returns:
            Optional[asyncio.Task]: The created task if successful, None otherwise
        """
        try:
            # Get the current Qt event loop
            loop = self._event_loop
            
            if not loop or loop.is_closed():
                self.logger.error("Event loop is not available or closed")
                return None
            
            # Create task and schedule it in the Qt event loop
            task = loop.create_task(coro)
            
            # Track task for cleanup
            event_loop_manager.track_task(task)
            
            # Set up error handling
            def _handle_task_result(task: asyncio.Task) -> None:
                try:
                    if task.exception():
                        self.logger.error(f"Task error: {task.exception()}")
                        self.error_occurred.emit(str(task.exception()))
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    self.logger.error(f"Error handling task result: {str(e)}")
            
            task.add_done_callback(_handle_task_result)
            
            return task
            
        except Exception as e:
            self.logger.error(f"Error in _run_coroutine: {str(e)}")
            return None
    
    async def add_message_async(self, text: str, is_user: bool = False, is_system: bool = False):
        """Async version of add_message."""
        self.add_message(text, is_user, is_system)
    
    def _handle_prompt(self, text: str):
        """Handle user prompt submission."""
        if text.strip():
            # Add message to chat
            self.add_message(text, is_user=True)
            self._update_state(last_command=text)
            
            # Send command to queue if available
            if self.command_queue:
                try:
                    self.command_queue.put(("command", text))
                    logger.debug(f"Sent command to queue: {text}")
                except Exception as e:
                    logger.error(f"Failed to send command to queue: {str(e)}")
                    self.add_message(f"Error sending command: {str(e)}", is_system=True)
                    return
            
            # Emit command received signal
            try:
                signal_manager = SignalManager()
                if signal_manager:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            signal_manager.emit(CoreSignal(
                                type=SignalType.COMMAND_RECEIVED,
                                data={"command": text},
                                source="gui"
                            )),
                            loop
                        )
                    else:
                        loop.run_until_complete(signal_manager.emit(CoreSignal(
                            type=SignalType.COMMAND_RECEIVED,
                            data={"command": text},
                            source="gui"
                        )))
            except Exception as e:
                logger.error(f"Failed to emit command signal: {str(e)}")
                self.add_message(f"Error emitting command signal: {str(e)}", is_system=True)
    
    def initialize_queues(self, command_queue: multiprocessing.Queue,
                         response_queue: multiprocessing.Queue,
                         on_prompt: Callable[[str], None]):
        """Initialize communication queues and callback with state tracking."""
        self.command_queue = command_queue
        self.response_queue = response_queue
        self.on_prompt = on_prompt
        
        # Register with concurrency manager first
        try:
            from tenire.organizers.concurrency import concurrency_manager
            if concurrency_manager:
                concurrency_manager._command_queue = command_queue
                concurrency_manager._response_queue = response_queue
        except Exception as e:
            logger.debug(f"Could not register queues with concurrency manager: {str(e)}")
        
        # Update state after registration
        self._update_state(status='queues_initialized')
    
    def __init__(self, width: int = 400, parent=None, process: Optional[GUIProcess] = None):
        """Initialize the main GUI widget."""
        super().__init__(parent)
        self.width = width
        self.command_queue = None
        self.response_queue = None
        self.on_prompt = None
        self._process = process  # Store reference to parent process
        self._signal_emitter = AsyncSignalEmitter()  # Use the shared signal emitter
        self._state = {
            'status': 'initializing',
            'is_ready': False,
            'error': None,
            'last_command': None,
            'last_response': None,
            'signal_handlers': set(),
            'cleanup_tasks': set(),
            'browser_commands': {},
            'browser_actions': {},
            'browser_state': {},
            'last_browser_command': None,
            'last_browser_action': None,
            'browser_errors': []
        }
        
        # Initialize logger
        self.logger = get_logger("gui.main_widget")
        
        # Set up qasync event loop with proper integration
        try:
            # Use the event loop from the process if available
            if self._process and hasattr(self._process, '_event_loop'):
                self._event_loop = self._process._event_loop
            else:
                from qasync import QEventLoop, QEventLoopPolicy
                asyncio.set_event_loop_policy(QEventLoopPolicy())
                self._event_loop = QEventLoop(self)
                asyncio.set_event_loop(self._event_loop)
                
            # Register with event loop manager
            event_loop_manager.register_process_main_loop(self._event_loop, is_qt=True)
            
            # Use existing universal timer
            self._timer = universal_timer
            
            # Register cleanup callback
            self.destroyed.connect(lambda: asyncio.create_task(self._cleanup_gui()))
            
        except ImportError:
            self.logger.warning("qasync not available, falling back to standard event loop")
            self._event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._event_loop)
            event_loop_manager.register_process_main_loop(self._event_loop, is_qt=False)
        
        # Get container and compactor references
        from tenire.core.container import container
        self.container = container
        self.compactor = container.get('compactor')
        
        # Set up the main layout
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        
        # Create widgets
        self.chat_widget = ChatWidget()
        self.input_widget = InputWidget()
        self.command_confirmation = CommandConfirmationWidget()
        self.safeguard_confirmation = SafeguardConfirmationWidget()
        
        # Add widgets to layout
        self.layout.addWidget(self.chat_widget)
        self.layout.addWidget(self.command_confirmation)
        self.layout.addWidget(self.safeguard_confirmation)
        self.layout.addWidget(self.input_widget)
        
        # Connect signals using qasync's asyncSlot
        from qasync import asyncSlot
        
        @asyncSlot()
        async def _handle_prompt_async(text):
            await self._handle_prompt(text)
            
        @asyncSlot()
        async def _handle_command_confirmation_async(confirmed):
            await self._handle_command_confirmation(confirmed)
            
        @asyncSlot()
        async def _handle_safeguard_confirmation_async(confirmed):
            await self._handle_safeguard_confirmation(confirmed)
        
        # Connect signals with async handlers
        self.input_widget.submit_signal.connect(_handle_prompt_async)
        self.command_confirmation.confirm_signal.connect(_handle_command_confirmation_async)
        self.safeguard_confirmation.confirm_signal.connect(_handle_safeguard_confirmation_async)
        
        # Connect internal signals
        self.message_received.connect(self._on_message_received)
        self.status_updated.connect(self._on_status_updated)
        self.command_processed.connect(self._on_command_processed)
        self.state_updated.connect(self._on_state_updated)
        self.error_occurred.connect(self._on_error_occurred)
        self.cleanup_requested.connect(self._on_cleanup_requested)
        
        # Set up logging handler
        self.logging_handler = SidebarHandler(self)
        logging.getLogger().addHandler(self.logging_handler)
        
        # Connect to framework signals
        self._connect_framework_signals()
        
        # Register cleanup tasks with compactor
        self._register_cleanup_tasks()
        
        # Set fixed width
        self.setFixedWidth(self.width)
        
        # Set style
        self.setStyleSheet("""
            QWidget {
                background-color: #1a1a1a;
            }
        """)
    
    def _connect_framework_signals(self) -> None:
        """Connect framework signals with proper error handling."""
        try:
            def register_handlers(signal_manager: SignalManager) -> None:
                handlers: List[Tuple[SignalType, Callable, int]] = [
                    (SignalType.GUI_UPDATE, self._handle_gui_update, 5),
                    (SignalType.GUI_ACTION, self._handle_gui_action, 5),
                    (SignalType.COMMAND_RECEIVED, self._handle_command_received, 10),
                    (SignalType.COMMAND_PROCESSED, self._handle_command_processed, 10),
                    (SignalType.COMMAND_COMPLETED, self._handle_command_completed, 10),
                    (SignalType.COMMAND_ERROR, self._handle_command_error, 10)
                ]
                
                for signal_type, handler, priority in handlers:
                    signal_manager.register_handler_sync(
                        signal_type,
                        handler,
                        priority=priority,
                        is_gui=True
                    )
                
                print("Connected framework signals successfully", file=sys.stdout)

            signal_manager = SignalManager()
            if signal_manager:
                register_handlers(signal_manager)
            else:
                print("Failed to get signal manager", file=sys.stderr)
                self._retry_connect_signals(attempt=1)
            
        except Exception as e:
            print(f"Error connecting framework signals: {str(e)}", file=sys.stderr)
            self._retry_connect_signals(attempt=1)

    def _retry_connect_signals(self, attempt: int, max_attempts: int = 3) -> None:
        """
        Retry connecting signals with exponential backoff.
        
        Args:
            attempt: Current attempt number
            max_attempts: Maximum number of attempts
        """
        if attempt <= max_attempts:
            delay = 2 ** (attempt - 1)
            QTimer.singleShot(delay * 1000, self._connect_framework_signals)

    async def _handle_error(self, error: Exception, context: str = "") -> None:
        """
        Centralized error handling with proper signal emission.
        
        Args:
            error: The exception that occurred
            context: Context where the error occurred
        """
        error_msg = f"Error in {context}: {str(error)}"
        print(error_msg, file=sys.stderr)
        
        self._state.update(status='error', error=error_msg)
        self.error_occurred.emit(error_msg)
        
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(
                SignalManager().emit(CoreSignal(
                    type=SignalType.GUI_ERROR,
                    data={"error": error_msg, "context": context},
                    source="gui"
                ))
            )

    @Slot(str)
    def _on_error_occurred(self, error_msg: str) -> None:
        """
        Handle error signal.
        
        Args:
            error_msg: The error message to display
        """
        self.add_message(f"Error: {error_msg}", is_system=True)
    
    async def _handle_gui_update(self, data: Dict[str, Any]) -> None:
        """
        Handle GUI update signals.
        
        Args:
            data: The update data containing type and message
        """
        update_type = data.get("update_type")
        if update_type == "status":
            await self.update_status(data.get("message", ""))
            self._update_state(status=data.get("message"))
        elif update_type == "message":
            await self.add_message(
                data.get("message", ""),
                data.get("is_user", False)
            )
            if not data.get("is_user", False):
                self._update_state(last_response=data.get("message"))
    
    async def _handle_gui_action(self, data: Dict[str, Any]) -> None:
        """
        Handle GUI action signals.
        
        Args:
            data: The action data containing type and parameters
        """
        action_type = data.get("action_type")
        if action_type == "clear":
            self.chat_widget.clear()
        elif action_type == "scroll":
            self.chat_widget.scroll_to_bottom()
    
    @Slot(str, bool)
    def _on_message_received(self, text: str, is_user: bool):
        """Handle received messages."""
        self.add_message(text, is_user)
    
    def add_message(self, text: str, is_user: bool = False, is_system: bool = False):
        """Add a message to the chat widget."""
        self.chat_widget.add_message(text, is_user, is_system)
        if not is_user:
            self._update_state(last_response=text)
    
    async def update_status(self, status: str):
        """Update GUI status."""
        self.add_message({"message": f"Status: {status}", "is_system": True}, is_system=True)
        self._update_state(status=status)
        await SignalManager().emit_gui_update("status", {"message": status})
    
    async def update_gui_state(self, state: Dict[str, Any]):
        """Update GUI state."""
        self._state.update(state)
        if state.get('error'):
            self.add_message(
                f"Error: {state['error']}", 
                is_system=True
            )
            await SignalManager().emit_gui_update("error", {"error": state['error']})
    
    def _handle_command_confirmation(self, confirmed: bool):
        """Handle command confirmation response."""
        if confirmed and self.command_queue:
            self.command_queue.put(("confirm_command", None))
            self._update_state(status='command_confirmed')
            asyncio.create_task(SignalManager().emit(CoreSignal(
                type=SignalType.COMMAND_CONFIRMATION,
                data={"confirmed": True},
                source="gui"
            )))
        else:
            self._update_state(status='command_rejected')
            asyncio.create_task(SignalManager().emit(CoreSignal(
                type=SignalType.COMMAND_CONFIRMATION,
                data={"confirmed": False},
                source="gui"
            )))
        self.command_confirmation.hide_command()
    
    def _handle_safeguard_confirmation(self, confirmed: bool, safeguard_type: str):
        """Handle safeguard confirmation response."""
        if confirmed and self.command_queue:
            self.command_queue.put(("confirm_safeguard", safeguard_type))
            self._update_state(status=f'safeguard_{safeguard_type}_confirmed')
            asyncio.create_task(SignalManager().emit(CoreSignal(
                type=SignalType.SAFEGUARD_CONFIRMATION,
                data={"type": safeguard_type, "confirmed": True},
                source="gui"
            )))
        else:
            self._update_state(status=f'safeguard_{safeguard_type}_rejected')
            asyncio.create_task(SignalManager().emit(CoreSignal(
                type=SignalType.SAFEGUARD_CONFIRMATION,
                data={"type": safeguard_type, "confirmed": False},
                source="gui"
            )))
        self.safeguard_confirmation.hide_safeguard()
    
    async def _cleanup_gui(self):
        """Clean up GUI resources with proper qasync handling."""
        try:
            # Update state
            self._update_state(status='cleaning_up')
            self._timer = None
            
            # Remove logging handler
            logging.getLogger().removeHandler(self.logging_handler)
            
            # Get current loop
            loop = self._event_loop
            if loop and not loop.is_closed():
                try:
                    # Cancel all tasks
                    tasks = asyncio.all_tasks(loop)
                    if tasks:
                        for task in tasks:
                            task.cancel()
                        
                        # Use qasync's gather for task cleanup
                        try:
                            await asyncio.wait_for(
                                asyncio.gather(*tasks, return_exceptions=True),
                                timeout=5.0
                            )
                        except asyncio.TimeoutError:
                            self.logger.warning("Timeout waiting for tasks to cancel")
                    
                    # Clear queues
                    if self.command_queue:
                        while not self.command_queue.empty():
                            try:
                                self.command_queue.get_nowait()
                            except queue.Empty:
                                break
                    
                    if self.response_queue:
                        while not self.response_queue.empty():
                            try:
                                self.response_queue.get_nowait()
                            except queue.Empty:
                                break
                    
                    # Clear references
                    self.command_queue = None
                    self.response_queue = None
                    self.on_prompt = None
                    
                    # Emit cleanup signal
                    await SignalManager().emit(CoreSignal(
                        type=SignalType.GUI_CLEANUP_REQUESTED,
                        data={"status": "cleanup_completed"},
                        source="gui"
                    ))
                    
                    # Final state update
                    self._update_state(
                        status='cleaned_up',
                        is_ready=False,
                        error=None,
                        last_command=None,
                        last_response=None
                    )
                    
                    # Stop and close the event loop
                    if isinstance(loop, QEventLoop):
                        loop.stop()
                        loop.close()
                    
                except Exception as e:
                    self.logger.error(f"Error cleaning up event loop: {str(e)}")
            
        except Exception as e:
            self.logger.error(f"Error during GUI cleanup: {str(e)}")
            self._update_state(status='error', error=str(e))
            try:
                await SignalManager().emit(CoreSignal(
                    type=SignalType.GUI_ERROR,
                    data={"error": str(e)},
                    source="gui"
                ))
            except Exception:
                pass  # Ignore errors during error reporting

    async def _cleanup_signals(self):
        """Clean up signal handlers."""
        for signal_type, handler_id in self._state['signal_handlers']:
            try:
                SignalManager().unregister_handler(signal_type, handler_id)
            except Exception as e:
                await self._handle_error(e, f"cleanup signal handler {signal_type}")
        self._state['signal_handlers'].clear()

    def _register_cleanup_tasks(self):
        """Register cleanup tasks with the compactor."""
        if not self.compactor:
            logger.warning("Compactor not available for cleanup task registration")
            return
            
        cleanup_tasks = [
            ("gui_widget_cleanup", self._cleanup_gui, 95),
            ("gui_signals_cleanup", self._cleanup_signals, 94),
            ("gui_logging_cleanup", self._cleanup_logging, 93),
            ("gui_queues_cleanup", self._cleanup_queues, 92)
        ]
        
        for name, func, priority in cleanup_tasks:
            task_name = f"main_gui_widget_{name}"
            self.compactor.register_cleanup_task(
                name=task_name,
                cleanup_func=func,
                priority=priority,
                is_async=True,
                metadata={"tags": ["gui", "widget"]}
            )
            self._state['cleanup_tasks'].add(task_name)
            
    async def _cleanup_logging(self):
        """Clean up logging handler."""
        try:
            if self.logging_handler:
                self.logging_handler.disable()
                logging.getLogger().removeHandler(self.logging_handler)
                self.logging_handler = None
        except Exception as e:
            await self._handle_error(e, "cleanup logging")
            
    async def _cleanup_queues(self):
        """Clean up command and response queues."""
        try:
            # Clear queues
            if self.command_queue:
                while not self.command_queue.empty():
                    try:
                        self.command_queue.get_nowait()
                    except queue.Empty:
                        break
            
            if self.response_queue:
                while not self.response_queue.empty():
                    try:
                        self.response_queue.get_nowait()
                    except queue.Empty:
                        break
                        
            # Clear references
            self.command_queue = None
            self.response_queue = None
            self.on_prompt = None
        except Exception as e:
            await self._handle_error(e, "cleanup queues")
            
    @Slot()
    def _handle_cleanup_request(self):
        """Handle cleanup request from Qt signal."""
        asyncio.create_task(self._cleanup_gui())
        
    def closeEvent(self, event):
        """Handle widget close event."""
        self.cleanup_requested.emit()
        event.accept()

    async def _handle_command_received(self, signal: CoreSignal) -> None:
        """Handle command received signal."""
        command = signal.data.get("command")
        if command:
            self.add_message(f"🔄 Processing command: {command}", is_system=True)
            self._update_state(status='processing_command', last_command=command)

    async def _handle_command_processed(self, signal: CoreSignal) -> None:
        """Handle command processed signal."""
        command = signal.data.get("command")
        if command:
            self.add_message(f"⚙️ Executing command: {command}", is_system=True)
            self._update_state(status='executing_command')

    async def _handle_command_completed(self, signal: CoreSignal) -> None:
        """Handle command completed signal."""
        command = signal.data.get("command")
        result = signal.data.get("result", {})
        success = signal.data.get("success", False)
        
        if command:
            if success:
                self.add_message(f"✅ Command completed: {command}", is_system=True)
                if "message" in result:
                    self.add_message(result["message"], is_system=True)
            else:
                self.add_message(f"⚠️ Command failed: {command}", is_system=True)
                if "error" in result:
                    self.add_message(f"Error: {result['error']}", is_system=True)
                    
            self._update_state(
                status='command_completed' if success else 'command_failed',
                last_response=result.get("message") or result.get("error")
            )

    async def _handle_command_error(self, signal: CoreSignal) -> None:
        """Handle command error signal."""
        command = signal.data.get("command")
        error = signal.data.get("error")
        result = signal.data.get("result", {})
        
        if command and error:
            self.add_message(f"❌ Command error: {command}", is_system=True)
            self.add_message(f"Error: {error}", is_system=True)
            
            self._update_state(
                status='command_error',
                error=error,
                last_response=result.get("message") or error
            )
            
            self.error_occurred.emit(str(error))

    async def _handle_browser_command_completed(self, signal: CoreSignal) -> None:
        """Handle browser command completion."""
        try:
            command = signal.data.get('command')
            result = signal.data.get('result')
            browser_state = signal.data.get('browser_state')
            
            if command and result:
                # Update command history
                self._state['browser_commands'][command].update({
                    'status': 'completed',
                    'result': result,
                    'completed_at': datetime.now().isoformat()
                })
                
                # Update browser state if provided
                if browser_state:
                    self._state['browser_state'].update(browser_state)
                
                # Update status
                await self._update_status(f"Browser command completed: {command}")
                
                # Display result if needed
                if isinstance(result, dict) and result.get('display'):
                    self.add_message(f"Browser command result: {result['display']}", is_system=True)
                    
                # Emit state update
                self.browser_state.emit(self._state['browser_state'])
                
        except Exception as e:
            await self._handle_error(e, "handle browser command completion")

    async def _handle_browser_action_completed(self, signal: CoreSignal) -> None:
        """Handle browser action completion."""
        try:
            action = signal.data.get('action')
            result = signal.data.get('result')
            browser_state = signal.data.get('browser_state')
            
            if action and result:
                # Update action history
                self._state['browser_actions'][action].update({
                    'status': 'completed',
                    'result': result,
                    'completed_at': datetime.now().isoformat()
                })
                
                # Update browser state if provided
                if browser_state:
                    self._state['browser_state'].update(browser_state)
                
                # Update status
                await self._update_status(f"Browser action completed: {action}")
                
                # Display result if needed
                if isinstance(result, dict) and result.get('display'):
                    self.add_message(f"Browser action result: {result['display']}", is_system=True)
                    
                # Emit state update
                self.browser_state.emit(self._state['browser_state'])
                
        except Exception as e:
            await self._handle_error(e, "handle browser action completion")

    async def _handle_browser_command(self, command: str, params: Dict[str, Any]) -> None:
        """Handle browser command execution."""
        try:
            # Ensure signal manager is initialized
            await self._initialize_signal_manager()
            
            # Update state
            self._state['last_browser_command'] = {
                'command': command,
                'params': params,
                'timestamp': datetime.now().isoformat()
            }
            
            # Emit command signal
            await self._signal_manager.emit(Signal(
                type=SignalType.BROWSER_COMMAND,
                data={
                    'command': command,
                    'params': params
                },
                source='gui'
            ))
            
            # Update status
            await self._update_status(f"Executing browser command: {command}")
            
            # Track command in history
            self._state['browser_commands'][command] = {
                'status': 'executing',
                'params': params,
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            await self._handle_error(e, f"handle browser command {command}")

    async def _handle_browser_action(self, action: str, params: Dict[str, Any]) -> None:
        """Handle browser action execution."""
        try:
            # Ensure signal manager is initialized
            await self._initialize_signal_manager()
            
            # Update state
            self._state['last_browser_action'] = {
                'action': action,
                'params': params,
                'timestamp': datetime.now().isoformat()
            }
            
            # Emit action signal
            await self._signal_manager.emit(Signal(
                type=SignalType.BROWSER_ACTION,
                data={
                    'action': action,
                    'params': params
                },
                source='gui'
            ))
            
            # Update status
            await self._update_status(f"Executing browser action: {action}")
            
            # Track action in history
            self._state['browser_actions'][action] = {
                'status': 'executing',
                'params': params,
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            await self._handle_error(e, f"handle browser action {action}")

    async def _update_status(self, status: str, error: bool = False) -> None:
        """Update status with proper signal emission."""
        try:
            self._state['status'] = status
            if error:
                self._state['error'] = status
                
            # Emit status update
            self.status_updated.emit(status)
            
            # Add status message
            self.add_message(f"Status: {status}", is_system=True)
            
            # Emit state update
            self.state_updated.emit(self._state)
            
        except Exception as e:
            logger.error(f"Error updating status: {str(e)}")
            
    async def _handle_error(self, error: Exception, context: str) -> None:
        """Handle errors with proper signal emission."""
        try:
            error_msg = f"Error in {context}: {str(error)}"
            logger.error(error_msg)
            
            # Ensure signal manager is initialized
            await self._initialize_signal_manager()
            
            # Emit error signal
            await self._signal_manager.emit(Signal(
                type=SignalType.GUI_ERROR,
                data={
                    'error': str(error),
                    'context': context
                },
                source='gui'
            ))
            
            # Update state
            self._state['error'] = error_msg
            self._state['status'] = 'error'
            
            # Update UI
            await self._update_status(error_msg, error=True)
            
        except Exception as e:
            logger.error(f"Error handling error: {str(e)}")
            # Fallback error handling
            self.error_occurred.emit(str(error))

    def _handle_future_result(self, future: asyncio.Future) -> None:
        """Handle the result of an async future."""
        try:
            if future.done():
                if future.exception():
                    logger.error(f"Error in async operation: {future.exception()}")
                else:
                    result = future.result()
                    if result:
                        logger.debug(f"Async operation completed: {result}")
        except Exception as e:
            logger.error(f"Error handling future result: {str(e)}")

    async def _initialize_signal_manager(self) -> None:
        """Initialize signal manager with retries."""
        if self._signal_manager is not None:
            return

        retry_count = 0
        max_retries = 3
        while retry_count < max_retries:
            try:
                from tenire.servicers.signal import get_signal_manager
                self._signal_manager = await get_signal_manager()
                if self._signal_manager is not None:
                    break
            except Exception as e:
                logger.warning(f"Attempt {retry_count + 1} to get signal manager failed: {str(e)}")
                retry_count += 1
                if retry_count < max_retries:
                    await asyncio.sleep(2 ** retry_count)

        if self._signal_manager is None:
            logger.error("Failed to initialize signal manager")
            raise RuntimeError("Signal manager initialization failed")

class SidebarGUI(QMainWindow):
    """Main window for the Tenire GUI."""
    
    def __init__(self, width: int = 400,
                 command_queue: Optional[multiprocessing.Queue] = None,
                 response_queue: Optional[multiprocessing.Queue] = None,
                 process: Optional[GUIProcess] = None):
        """Initialize the main window."""
        super().__init__()
        
        # Store process reference
        self._process = process
        
        # Create main widget with process reference
        self.main_widget = MainGUIWidget(
            width=width,
            parent=self,
            process=self._process
        )
        self.setCentralWidget(self.main_widget)
        
        # Initialize queues if provided
        if command_queue and response_queue:
            self.main_widget.initialize_queues(command_queue, response_queue, self._handle_prompt)
        
        # Set window properties
        self.setWindowTitle("Tenire")
        self.setFixedWidth(width)
        
        # Register with compactor
        compactor.register_cleanup_task(
            name="sidebar_gui_cleanup",
            cleanup_func=self._cleanup_gui,
            priority=95,
            is_async=True,
            metadata={"tags": ["gui"]}
        )
        
        # Set style
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1a1a1a;
            }
        """)
        
        # Initialize signal connections
        self._initialize_signal_connections()
        
        # Connect to process signals if available
        if self._process:
            self._connect_process_signals()
    
    def _connect_process_signals(self):
        """Connect to process-specific signals."""
        if not self._process:
            return
            
        try:
            # Connect process state updates
            self._process._startup_event.add_done_callback(
                lambda _: self.main_widget._update_state(status='process_ready')
            )
            self._process._shutdown_event.add_done_callback(
                lambda _: self.main_widget.cleanup_requested.emit()
            )
            
            # Use process's signal emitter for browser signals
            self.main_widget.browser_command.connect(
                lambda cmd, params: self._process._signal_emitter.emit_signal(
                    CoreSignal(
                        type=SignalType.BROWSER_COMMAND,
                        data={"command": cmd, "params": params},
                        source="gui"
                    )
                )
            )
            
            self.main_widget.browser_action.connect(
                lambda action, params: self._process._signal_emitter.emit_signal(
                    CoreSignal(
                        type=SignalType.BROWSER_ACTION,
                        data={"action": action, "params": params},
                        source="gui"
                    )
                )
            )
            
        except Exception as e:
            self.main_widget.logger.error(f"Error connecting process signals: {str(e)}")
    
    def _handle_prompt(self, text: str):
        """Handle user prompt submission."""
        if self._process:
            self._process.command_queue.put(("command", text))
        else:
            self.main_widget._handle_prompt(text)
    
    async def _cleanup_gui(self):
        """Clean up GUI resources."""
        await self.main_widget._cleanup_gui()
        
        # Notify process of cleanup if available
        if self._process:
            try:
                self._process._shutdown_event.set()
            except Exception as e:
                self.main_widget.logger.error(f"Error notifying process of cleanup: {str(e)}")
    
    def closeEvent(self, event):
        """Handle window close event."""
        asyncio.create_task(self._cleanup_gui())
        event.accept()

    def _initialize_signal_connections(self):
        """Initialize signal connections between GUI components."""
        try:
            # Connect main widget signals to window methods
            self.main_widget.message_received.connect(self.add_message)
            self.main_widget.status_updated.connect(self._on_status_updated)
            self.main_widget.command_processed.connect(self._on_command_processed)
            self.main_widget.state_updated.connect(self._on_state_updated)
            self.main_widget.error_occurred.connect(self._on_error_occurred)
            self.main_widget.cleanup_requested.connect(self._cleanup_gui)
            
            logger.debug("Initialized GUI signal connections")
            
        except Exception as e:
            logger.error(f"Error initializing signal connections: {str(e)}")
            raise

    def add_message(self, text: str, is_user: bool = False, is_system: bool = False):
        """Add a message to the chat."""
        self.main_widget.add_message(text, is_user, is_system)
    
    async def update_status(self, status: str):
        """Update GUI status."""
        await self.main_widget.update_status(status)
    
    async def update_gui_state(self, state: Dict[str, Any]):
        """Update GUI state."""
        await self.main_widget.update_gui_state(state)
    
    @Slot(str)
    def _on_status_updated(self, status: str) -> None:
        """
        Handle status updates.
        
        Args:
            status: The new status message
        """
        self.main_widget._on_status_updated(status)

    @Slot(dict)
    def _on_command_processed(self, result: Dict[str, Any]) -> None:
        """
        Handle command processed signal.
        
        Args:
            result: The command processing result
        """
        self.main_widget._on_command_processed(result)

    @Slot(dict)
    def _on_state_updated(self, state: Dict[str, Any]) -> None:
        """
        Handle state updates.
        
        Args:
            state: The new state dictionary
        """
        self.main_widget._on_state_updated(state)

    @Slot(str)
    def _on_error_occurred(self, error_msg: str) -> None:
        """
        Handle error signal.
        
        Args:
            error_msg: The error message to display
        """
        self.main_widget._on_error_occurred(error_msg)

class SidebarHandler(logging.Handler):
    """Logging handler that sends messages to the sidebar GUI."""
    
    def __init__(self, sidebar: 'MainGUIWidget'):
        """Initialize the handler."""
        super().__init__()
        self.sidebar = sidebar
        
    def emit(self, record: logging.LogRecord):
        """Emit a log record to the sidebar."""
        try:
            msg = self.format(record)
            self.sidebar.add_message(msg, False)
        except Exception:
            self.handleError(record) 
            self.handleError(record) 
            self.handleError(record) 