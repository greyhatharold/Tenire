"""
GUI module for the Tenire framework.

This module provides the graphical user interface components
and handlers for the framework.
"""

from .processes.gui_process import GUIProcess
from .gui import SidebarGUI, SidebarHandler
from .widgets import (
    ChatWidget,
    InputWidget,
    CommandConfirmationWidget,
    SafeguardConfirmationWidget
)

__all__ = [
    'GUIProcess',
    'SidebarGUI',
    'SidebarHandler',
    'ChatWidget',
    'InputWidget',
    'CommandConfirmationWidget',
    'SafeguardConfirmationWidget'
] 