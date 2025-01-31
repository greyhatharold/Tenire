"""
Logging handler for sending messages to the sidebar GUI.
"""

import logging
from typing import TYPE_CHECKING, Optional
from tenire.utils.logger import get_logger

if TYPE_CHECKING:
    from tenire.gui.gui import SidebarGUI

logger = get_logger(__name__)

class SidebarHandler(logging.Handler):
    """
    Logging handler that sends messages to the sidebar GUI.
    
    This handler integrates with the existing logging system to
    display log messages in the sidebar.
    """
    
    def __init__(self, sidebar: 'SidebarGUI'):
        """
        Initialize the handler.
        
        Args:
            sidebar: SidebarGUI instance to send messages to
        """
        super().__init__()
        self.sidebar = sidebar
        self._enabled: bool = True
        self._error_count: int = 0
        self.MAX_ERRORS = 3  # Maximum number of consecutive errors before disabling
        
    def emit(self, record: logging.LogRecord):
        """
        Emit a log record to the sidebar.
        
        Args:
            record: Log record to emit
        """
        if not self._enabled:
            return
            
        try:
            msg = self.format(record)
            self.sidebar.add_message(msg, False)
            self._error_count = 0  # Reset error count on success
        except Exception as e:
            self._error_count += 1
            if self._error_count >= self.MAX_ERRORS:
                self._enabled = False
                logger.error(f"Disabling SidebarHandler after {self.MAX_ERRORS} consecutive errors")
            self.handleError(record)
            
    def enable(self):
        """Enable the handler."""
        self._enabled = True
        self._error_count = 0
        
    def disable(self):
        """Disable the handler."""
        self._enabled = False 