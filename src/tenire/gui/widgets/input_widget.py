"""
Widget for user input with submit button functionality.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QPushButton
from PySide6.QtCore import Qt, Signal

class InputWidget(QWidget):
    """Widget for user input with submit button."""
    
    submit_signal = Signal(str)
    
    def __init__(self, parent=None):
        """Initialize input widget with text area and button."""
        super().__init__(parent)
        
        # Create layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 10)
        
        # Create text edit
        self.input_text = QTextEdit()
        self.input_text.setPlaceholderText("Type your message...")
        self.input_text.setMinimumHeight(100)
        self.input_text.setMaximumHeight(100)
        self.input_text.setStyleSheet("""
            QTextEdit {
                background-color: #2b2b2b;
                color: #ffffff;
                border: none;
                border-radius: 10px;
                padding: 10px;
            }
        """)
        
        # Create submit button
        self.submit_btn = QPushButton("Submit")
        self.submit_btn.setStyleSheet("""
            QPushButton {
                background-color: #0d6efd;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #0b5ed7;
            }
            QPushButton:pressed {
                background-color: #0a58ca;
            }
        """)
        
        # Add widgets to layout
        layout.addWidget(self.input_text)
        layout.addWidget(self.submit_btn, alignment=Qt.AlignRight)
        
        # Connect signals
        self.submit_btn.clicked.connect(self._handle_submit)
        self.input_text.textChanged.connect(self._handle_text_changed)
        
    def _handle_submit(self):
        """Handle submit button click."""
        text = self.input_text.toPlainText().strip()
        if text:
            self.submit_signal.emit(text)
            self.input_text.clear()
            
    def _handle_text_changed(self):
        """Handle text changes to enable/disable submit button."""
        self.submit_btn.setEnabled(bool(self.input_text.toPlainText().strip()))
        
    def keyPressEvent(self, event):
        """Handle key press events for submission."""
        if event.key() == Qt.Key_Return and event.modifiers() == Qt.ControlModifier:
            self._handle_submit()
        else:
            super().keyPressEvent(event) 