"""
Tenire Framework

A comprehensive framework for automated betting strategies and analysis.
"""

# Package-level constants that might be needed across modules
DEFAULT_TIMEOUT = 30  # seconds
API_VERSION = 'v1'

__version__ = "0.1.0"
__author__ = "Griffin Strier"
__email__ = "gjstrier@gmail.com"

# Import only what's needed for initialization
from tenire.utils.logger import get_logger

# Initialize logger first
logger = get_logger(__name__)

# Then import container
from tenire.core.container import container

# The rest of the imports will be handled by the modules that need them
__all__ = [
    'container',
    'logger',
    'DEFAULT_TIMEOUT',
    'API_VERSION',
    '__version__',
    '__author__',
    '__email__'
]
