"""
Widgets for command and safeguard confirmations.
"""

import asyncio
from typing import Optional, Dict, Any
from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox
from PySide6.QtCore import Qt, Signal as QtSignal

from tenire.core.codex import Signal, SignalType
from tenire.servicers import SignalManager
from tenire.structure.component_types import ComponentType
from tenire.utils.logger import get_logger
from tenire.core.event_loop import event_loop_manager

class CommandConfirmationWidget(QFrame):
    """Widget for displaying and confirming pending commands."""
    
    confirm_signal = QtSignal(bool)  # True for confirm, False for reject
    
    def __init__(self, parent: Optional[QFrame] = None):
        """Initialize command confirmation widget."""
        super().__init__(parent)
        self.logger = get_logger("gui.widgets.command_confirmation")
        self.signal_manager = SignalManager()
        self._current_command: Optional[str] = None
        
        # Schedule async registration
        self._schedule_registration()
        
        self.setFrameStyle(QFrame.StyledPanel)
        self.setVisible(False)  # Hidden by default
        
        # Create layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        
        # Create command display with better formatting
        self.command_label = QLabel()
        self.command_label.setWordWrap(True)
        self.command_label.setTextFormat(Qt.RichText)
        self.command_label.setStyleSheet("""
            QLabel {
                color: #ffffff;
                font-size: 13px;
                padding: 5px;
                background-color: #363636;
                border-radius: 5px;
            }
        """)
        
        # Create buttons
        button_layout = QHBoxLayout()
        self.confirm_btn = QPushButton("Confirm (⌘⏎)")
        self.reject_btn = QPushButton("Reject (Esc)")
        
        # Style buttons
        self.confirm_btn.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 16px;
                font-weight: bold;
                min-width: 120px;
            }
            QPushButton:hover {
                background-color: #218838;
            }
            QPushButton:focus {
                outline: none;
                border: 2px solid #1e7e34;
            }
        """)
        
        self.reject_btn.setStyleSheet("""
            QPushButton {
                background-color: #dc3545;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 16px;
                font-weight: bold;
                min-width: 120px;
            }
            QPushButton:hover {
                background-color: #c82333;
            }
            QPushButton:focus {
                outline: none;
                border: 2px solid #bd2130;
            }
        """)
        
        # Add widgets to layouts
        button_layout.addWidget(self.confirm_btn)
        button_layout.addWidget(self.reject_btn)
        button_layout.setSpacing(10)
        
        layout.addWidget(self.command_label)
        layout.addLayout(button_layout)
        
        # Connect signals
        self.confirm_btn.clicked.connect(lambda: self._handle_button_click(True))
        self.reject_btn.clicked.connect(lambda: self._handle_button_click(False))
        
        # Style frame
        self.setStyleSheet("""
            QFrame {
                background-color: #2b2b2b;
                border-radius: 10px;
                margin: 5px 10px;
            }
        """)
        
        # Set focus policy
        self.setFocusPolicy(Qt.StrongFocus)
        self.confirm_btn.setFocusPolicy(Qt.StrongFocus)
        self.reject_btn.setFocusPolicy(Qt.StrongFocus)
    
    def _schedule_registration(self) -> None:
        """Schedule the async component registration."""
        import asyncio
        from functools import partial
        from tenire.core.container import container
        from tenire.core.event_loop import event_loop_manager
        
        def _safe_register():
            """Safely register component without external dependencies."""
            try:
                # Get event loop through manager
                loop = event_loop_manager.get_event_loop(for_qt=True)
                
                # Create and track registration task
                coro = self._register_component()
                if loop.is_running():
                    task = asyncio.create_task(coro)
                    event_loop_manager.track_task(task)
                    # Add done callback to handle completion
                    task.add_done_callback(
                        lambda _: self.logger.debug("Component registration completed")
                    )
                else:
                    # If loop is not running, run the coroutine directly
                    try:
                        loop.run_until_complete(coro)
                    except RuntimeError as e:
                        if "Event loop is closed" in str(e):
                            # Create new loop if current one is closed
                            new_loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(new_loop)
                            task = new_loop.create_task(coro)
                            event_loop_manager.track_task(task)
                            try:
                                new_loop.run_until_complete(task)
                            finally:
                                if not new_loop.is_closed():
                                    new_loop.close()
                        else:
                            raise
                            
            except Exception as e:
                self.logger.error(f"Registration error: {str(e)}")
                return None

        # Run registration in Qt's event loop
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, _safe_register)

    async def _register_component(self) -> None:
        """Register this widget as a GUI component."""
        try:
            # Try importing PipelineRegistry first
            try:
                from tenire.structure.component_registry import PipelineRegistry
                registry = PipelineRegistry()
            except ImportError:
                # Fall back to ComponentRegistry if PipelineRegistry not available
                from tenire.structure.component_registry import ComponentRegistry
                registry = ComponentRegistry()
                self.logger.warning("Using ComponentRegistry fallback for registration")

            await registry.register_component(
                name="command_confirmation_widget",
                component=self,
                component_type=ComponentType.GUI,
                provides=["command_confirmation"],
                dependencies=set(["signal_manager"]),
                priority=70,
                health_checks={
                    "widget_visible": self.isVisible,
                    "has_current_command": lambda: self._current_command is not None
                },
                tags={"gui", "confirmation", "command"}
            )
            self.logger.debug("Command confirmation widget registered successfully")
        except Exception as e:
            self.logger.error(f"Failed to register command confirmation widget: {str(e)}")

    async def show_command(self, command: str) -> None:
        """Display a command and show the widget."""
        self._current_command = command
        formatted_command = f"""
            <p style='margin-bottom: 10px;'><b>Confirm action:</b></p>
            <p style='color: #00ff00; margin-left: 10px;'>{command}</p>
        """
        self.command_label.setText(formatted_command)
        self.setVisible(True)
        self.confirm_btn.setFocus()
        
        # Emit signal for command display
        await self.signal_manager.emit(Signal(
            type=SignalType.GUI_COMMAND_DISPLAYED,
            data={
                "command": command,
                "widget": "command_confirmation"
            }
        ))
    
    async def hide_command(self) -> None:
        """Hide the widget and reset state."""
        self.setVisible(False)
        old_command = self._current_command
        self._current_command = None
        
        # Emit signal for command hide
        if old_command:
            await self.signal_manager.emit(Signal(
                type=SignalType.GUI_COMMAND_HIDDEN,
                data={
                    "previous_command": old_command,
                    "widget": "command_confirmation"
                }
            ))
    
    async def _handle_button_click(self, is_confirm: bool):
        """Handle button clicks by creating async tasks."""
        if self._current_command:
            await self.signal_manager.emit(Signal(
                type=SignalType.GUI_COMMAND_CONFIRMED if is_confirm else SignalType.GUI_COMMAND_REJECTED,
                data={
                    "command": self._current_command,
                    "widget": "command_confirmation"
                }
            ))
            self.confirm_signal.emit(is_confirm)
    
    def keyPressEvent(self, event):
        """Handle keyboard shortcuts."""
        if event.key() == Qt.Key_Return and event.modifiers() & Qt.ControlModifier:
            # Create and track the task
            task = asyncio.create_task(self._handle_button_click(True))
            event_loop_manager.track_task(task)
        elif event.key() == Qt.Key_Escape:
            # Create and track the task
            task = asyncio.create_task(self._handle_button_click(False))
            event_loop_manager.track_task(task)
        else:
            super().keyPressEvent(event)

class SafeguardConfirmationWidget(QFrame):
    """Widget for displaying and confirming safeguard-related actions."""
    
    confirm_signal = QtSignal(bool, str)  # Confirmation status and safeguard type
    
    def __init__(self, parent: Optional[QFrame] = None):
        """Initialize safeguard confirmation widget."""
        super().__init__(parent)
        self.logger = get_logger("gui.widgets.safeguard_confirmation")
        self.signal_manager = SignalManager()
        self._current_safeguard: Optional[str] = None
        
        # Schedule async registration
        self._schedule_registration()
        
        self.setFrameStyle(QFrame.StyledPanel)
        self.setVisible(False)  # Hidden by default
        
        # Create layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        
        # Create warning icon and title
        title_layout = QHBoxLayout()
        self.warning_icon = QLabel("⚠️")
        self.warning_icon.setStyleSheet("""
            QLabel {
                color: #ffc107;
                font-size: 24px;
            }
        """)
        self.title_label = QLabel("Safeguard Check")
        self.title_label.setStyleSheet("""
            QLabel {
                color: #ffc107;
                font-size: 16px;
                font-weight: bold;
            }
        """)
        title_layout.addWidget(self.warning_icon)
        title_layout.addWidget(self.title_label)
        title_layout.addStretch()
        
        # Create message display
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        self.message_label.setTextFormat(Qt.RichText)
        self.message_label.setStyleSheet("""
            QLabel {
                color: #ffffff;
                font-size: 13px;
                padding: 10px;
                background-color: #363636;
                border-radius: 5px;
                margin: 5px 0;
            }
        """)
        
        # Create checkbox for "Don't ask again"
        self.dont_ask_checkbox = QCheckBox("Don't ask again for this session")
        self.dont_ask_checkbox.setStyleSheet("""
            QCheckBox {
                color: #ffffff;
                font-size: 12px;
                margin-top: 5px;
            }
        """)
        
        # Create buttons
        button_layout = QHBoxLayout()
        self.confirm_btn = QPushButton("Proceed (⌘⏎)")
        self.reject_btn = QPushButton("Cancel (Esc)")
        
        # Style buttons
        self.confirm_btn.setStyleSheet("""
            QPushButton {
                background-color: #ffc107;
                color: black;
                border: none;
                border-radius: 5px;
                padding: 8px 16px;
                font-weight: bold;
                min-width: 120px;
            }
            QPushButton:hover {
                background-color: #e0a800;
            }
            QPushButton:focus {
                outline: none;
                border: 2px solid #d39e00;
            }
        """)
        
        self.reject_btn.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 16px;
                font-weight: bold;
                min-width: 120px;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
            QPushButton:focus {
                outline: none;
                border: 2px solid #545b62;
            }
        """)
        
        # Add widgets to layouts
        button_layout.addWidget(self.confirm_btn)
        button_layout.addWidget(self.reject_btn)
        button_layout.setSpacing(10)
        
        layout.addLayout(title_layout)
        layout.addWidget(self.message_label)
        layout.addWidget(self.dont_ask_checkbox)
        layout.addLayout(button_layout)
        
        # Connect signals
        self.confirm_btn.clicked.connect(lambda: self._handle_button_click(True))
        self.reject_btn.clicked.connect(lambda: self._handle_button_click(False))
        
        # Style frame
        self.setStyleSheet("""
            QFrame {
                background-color: #2b2b2b;
                border: 2px solid #ffc107;
                border-radius: 10px;
                margin: 5px 10px;
            }
        """)
        
        # Set focus policy
        self.setFocusPolicy(Qt.StrongFocus)
        self.confirm_btn.setFocusPolicy(Qt.StrongFocus)
        self.reject_btn.setFocusPolicy(Qt.StrongFocus)
    
    def _schedule_registration(self) -> None:
        """Schedule the async component registration."""
        import asyncio
        from functools import partial
        from tenire.core.container import container
        from tenire.core.event_loop import event_loop_manager
        
        def _safe_register():
            """Safely register component without external dependencies."""
            try:
                # Get event loop through manager
                loop = event_loop_manager.get_event_loop(for_qt=True)
                
                # Create and track registration task
                coro = self._register_component()
                if loop.is_running():
                    task = asyncio.create_task(coro)
                    event_loop_manager.track_task(task)
                    # Add done callback to handle completion
                    task.add_done_callback(
                        lambda _: self.logger.debug("Component registration completed")
                    )
                else:
                    # If loop is not running, run the coroutine directly
                    try:
                        loop.run_until_complete(coro)
                    except RuntimeError as e:
                        if "Event loop is closed" in str(e):
                            # Create new loop if current one is closed
                            new_loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(new_loop)
                            task = new_loop.create_task(coro)
                            event_loop_manager.track_task(task)
                            try:
                                new_loop.run_until_complete(task)
                            finally:
                                if not new_loop.is_closed():
                                    new_loop.close()
                        else:
                            raise
                            
            except Exception as e:
                self.logger.error(f"Registration error: {str(e)}")
                return None

        # Run registration in Qt's event loop
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, _safe_register)

    async def _register_component(self) -> None:
        """Register this widget as a GUI component."""
        try:
            # Try importing PipelineRegistry first
            try:
                from tenire.structure.component_registry import PipelineRegistry
                registry = PipelineRegistry()
            except ImportError:
                # Fall back to ComponentRegistry if PipelineRegistry not available
                from tenire.structure.component_registry import ComponentRegistry
                registry = ComponentRegistry()
                self.logger.warning("Using ComponentRegistry fallback for registration")

            await registry.register_component(
                name="safeguard_confirmation_widget",
                component=self,
                component_type=ComponentType.GUI,
                provides=["safeguard_confirmation"],
                dependencies=set(["signal_manager"]),
                priority=70,
                health_checks={
                    "widget_visible": self.isVisible,
                    "has_current_safeguard": lambda: self._current_safeguard is not None
                },
                tags={"gui", "confirmation", "safeguard"}
            )
            self.logger.debug("Safeguard confirmation widget registered successfully")
        except Exception as e:
            self.logger.error(f"Failed to register safeguard confirmation widget: {str(e)}")

    async def show_safeguard(self, safeguard_type: str, message: str) -> None:
        """Display a safeguard confirmation and show the widget."""
        self._current_safeguard = safeguard_type
        
        # Format title based on safeguard type
        title = safeguard_type.replace('_', ' ').title()
        self.title_label.setText(f"Safeguard Check: {title}")
        
        # Format message with warning icon
        formatted_message = f"""
            <p style='margin-bottom: 10px;'><b>Confirmation Required:</b></p>
            <p style='color: #ffc107; margin-left: 10px;'>⚠️ {message}</p>
        """
        self.message_label.setText(formatted_message)
        self.setVisible(True)
        self.confirm_btn.setFocus()
        
        # Emit signal for safeguard display
        await self.signal_manager.emit(Signal(
            type=SignalType.GUI_SAFEGUARD_DISPLAYED,
            data={
                "safeguard_type": safeguard_type,
                "message": message,
                "widget": "safeguard_confirmation"
            }
        ))
    
    async def hide_safeguard(self) -> None:
        """Hide the widget and reset state."""
        self.setVisible(False)
        old_safeguard = self._current_safeguard
        self._current_safeguard = None
        self.dont_ask_checkbox.setChecked(False)
        
        # Emit signal for safeguard hide
        if old_safeguard:
            await self.signal_manager.emit(Signal(
                type=SignalType.GUI_SAFEGUARD_HIDDEN,
                data={
                    "previous_safeguard": old_safeguard,
                    "widget": "safeguard_confirmation"
                }
            ))
    
    async def _handle_button_click(self, is_confirm: bool):
        """Handle button clicks by creating async tasks."""
        if self._current_safeguard:
            await self.signal_manager.emit(Signal(
                type=SignalType.GUI_SAFEGUARD_CONFIRMED if is_confirm else SignalType.GUI_SAFEGUARD_REJECTED,
                data={
                    "safeguard_type": self._current_safeguard,
                    "dont_ask_again": self.dont_ask_checkbox.isChecked(),
                    "widget": "safeguard_confirmation"
                }
            ))
            self.confirm_signal.emit(is_confirm, self._current_safeguard)
    
    def keyPressEvent(self, event):
        """Handle keyboard shortcuts."""
        if event.key() == Qt.Key_Return and event.modifiers() & Qt.ControlModifier:
            # Create and track the task
            task = asyncio.create_task(self._handle_button_click(True))
            event_loop_manager.track_task(task)
        elif event.key() == Qt.Key_Escape:
            # Create and track the task
            task = asyncio.create_task(self._handle_button_click(False))
            event_loop_manager.track_task(task)
        else:
            super().keyPressEvent(event) 