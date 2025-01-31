"""
GUI process management for the Tenire framework.

This module handles the GUI process lifecycle and communication
with the main application process.
"""

import multiprocessing
import logging
from typing import Optional, Dict, Any, Callable

from PySide6.QtWidgets import QApplication
import qasync

from tenire.core.codex import Signal, SignalType
from tenire.servicers import SignalManager
from tenire.utils.logger import get_logger

logger = get_logger(__name__)

class GUIProcess:
    """Manages the GUI process and its communication with the main process."""
    
    def __init__(self, width: int = 400):
        """Initialize the GUI process manager."""
        self.width = width
        self.process: Optional[multiprocessing.Process] = None
        self.command_queue: Optional[multiprocessing.Queue] = None
        self.response_queue: Optional[multiprocessing.Queue] = None
        self.signal_manager = SignalManager()
        self._is_running = False
        
    def start(self) -> None:
        """Start the GUI process."""
        if self._is_running:
            logger.warning("GUI process already running")
            return
            
        self.command_queue = multiprocessing.Queue()
        self.response_queue = multiprocessing.Queue()
        
        self.process = multiprocessing.Process(
            target=self._run_gui,
            args=(self.command_queue, self.response_queue, self.width),
            daemon=True
        )
        
        self.process.start()
        self._is_running = True
        
        logger.info("Started GUI process")
        
    def stop(self) -> None:
        """Stop the GUI process."""
        if not self._is_running:
            return
            
        if self.process and self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=5.0)
            
        self._cleanup_queues()
        self._is_running = False
        logger.info("Stopped GUI process")
        
    def is_alive(self) -> bool:
        """Check if the GUI process is alive."""
        return self.process is not None and self.process.is_alive()
        
    def _cleanup_queues(self) -> None:
        """Clean up the communication queues."""
        if self.command_queue:
            while not self.command_queue.empty():
                try:
                    self.command_queue.get_nowait()
                except:
                    pass
            self.command_queue = None
            
        if self.response_queue:
            while not self.response_queue.empty():
                try:
                    self.response_queue.get_nowait()
                except:
                    pass
            self.response_queue = None
            
    @staticmethod
    def _run_gui(command_queue: multiprocessing.Queue,
                response_queue: multiprocessing.Queue,
                width: int) -> None:
        """Run the GUI application in a separate process."""
        try:
            # Create Qt application
            app = QApplication([])
            
            # Create event loop
            loop = qasync.QEventLoop(app)
            
            # Import here to avoid circular imports
            from tenire.gui import SidebarGUI
            
            # Create main window
            window = SidebarGUI(
                width=width,
                command_queue=command_queue,
                response_queue=response_queue
            )
            window.show()
            
            # Run event loop
            loop.run_forever()
            
        except Exception as e:
            logger.error(f"Error in GUI process: {str(e)}")
            raise
            
    async def cleanup(self) -> None:
        """Clean up resources."""
        try:
            self.stop()
            await self.signal_manager.emit(Signal(
                type=SignalType.GUI_CLEANUP_COMPLETED,
                data={"component": "gui_process"},
                source="gui"
            ))
        except Exception as e:
            logger.error(f"Error during GUI process cleanup: {str(e)}")
            await self.signal_manager.emit(Signal(
                type=SignalType.GUI_CLEANUP_ERROR,
                data={
                    "component": "gui_process",
                    "error": str(e)
                },
                source="gui"
            ))
            raise 