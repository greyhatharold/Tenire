"""
Widget for displaying the chat history with scrolling capability.
"""

# Standard library imports
from datetime import datetime

# Third-party imports
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QScrollArea,
    QVBoxLayout,
    QWidget
)

# Local imports 
from .message_widget import MessageWidget

class ChatWidget(QScrollArea):
    """Widget for displaying the chat history."""
    
    def __init__(self, parent=None):
        """Initialize chat widget with scrollable area."""
        super().__init__(parent)
        
        # Create container widget and layout
        self.container = QWidget()
        self.layout = QVBoxLayout(self.container)
        self.layout.setSpacing(10)
        self.layout.addStretch()
        
        # Configure scroll area
        self.setWidget(self.container)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: #1a1a1a;
            }
            QScrollBar:vertical {
                border: none;
                background-color: #2b2b2b;
                width: 10px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background-color: #404040;
                min-height: 20px;
                border-radius: 5px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                border: none;
                background: none;
            }
        """)
        
    def add_message(self, text: str, is_user: bool = False, is_system: bool = False):
        """Add a new message to the chat."""
        # Remove stretch from layout
        self.layout.takeAt(self.layout.count() - 1)
        
        # Add timestamp and message
        timestamp = datetime.now().strftime("%H:%M:%S")
        msg_widget = MessageWidget(f"[{timestamp}] {text}", is_user, is_system)
        self.layout.addWidget(msg_widget)
        
        # Add stretch back
        self.layout.addStretch()
        
        # Scroll to bottom
        self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()
        )
        
    def clear(self):
        """Clear all messages from the chat."""
        while self.layout.count() > 1:  # Keep the stretch at the end
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
    def scroll_to_bottom(self):
        """Scroll the chat view to the bottom."""
        self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()
        ) 