"""
Widget for displaying individual messages in the chat interface.
"""

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel
from PySide6.QtCore import Qt

class MessageWidget(QFrame):
    """Widget for displaying a single message in the chat."""
    
    def __init__(self, text: str, is_user: bool = False, is_system: bool = False, parent=None):
        """Initialize message widget with text and style."""
        super().__init__(parent)
        self.setFrameStyle(QFrame.StyledPanel)
        
        # Create layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        
        # Create message label
        self.message = QLabel(text)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        
        # Set style based on message type
        if is_user:
            self.setStyleSheet("""
                QFrame {
                    background-color: #2b2b2b;
                    border-radius: 10px;
                    margin-left: 50px;
                }
                QLabel {
                    color: #ffffff;
                }
            """)
            layout.addStretch()
            layout.addWidget(self.message)
        elif is_system:
            self.setStyleSheet("""
                QFrame {
                    background-color: #1a1a2e;
                    border-radius: 10px;
                    margin-right: 50px;
                    border: 1px solid #2a2a4e;
                }
                QLabel {
                    color: #8f8fff;
                    font-style: italic;
                }
            """)
            layout.addWidget(self.message)
            layout.addStretch()
        else:
            self.setStyleSheet("""
                QFrame {
                    background-color: #1e1e1e;
                    border-radius: 10px;
                    margin-right: 50px;
                }
                QLabel {
                    color: #ffffff;
                }
            """)
            layout.addWidget(self.message)
            layout.addStretch() 