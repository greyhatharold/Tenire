"""
Actions package for the Tenire framework.

This package contains modules for processing and executing commands and actions
within the framework.
"""

from tenire.actions.command_processor import (
    CommandProcessor,
    CommandType,
    ParsedCommand,
    CommandParseError,
    ProcessorConfig,
    processor_config
)

# Initialize shared context
_shared_context = None

def get_shared_context():
    """Get the shared browser context."""
    global _shared_context
    return _shared_context

def set_shared_context(context):
    """Set the shared browser context."""
    global _shared_context
    _shared_context = context

# Export config values for backward compatibility
COMMAND_TIMEOUT = processor_config.command_timeout
INIT_TIMEOUT = processor_config.init_timeout
MAX_RETRIES = processor_config.max_retries
RETRY_DELAY = processor_config.retry_delay
MAX_QUEUE_SIZE = processor_config.max_queue_size

__all__ = [
    'COMMAND_TIMEOUT',
    'INIT_TIMEOUT',
    'MAX_RETRIES',
    'RETRY_DELAY',
    'MAX_QUEUE_SIZE',
    'CommandProcessor',
    'CommandType',
    'ParsedCommand',
    'CommandParseError',
    'get_shared_context',
    'set_shared_context'
]
