"""
Component type definitions for the Tenire framework.

This module contains the component type enums and related metadata
to avoid circular dependencies between GUI and registry components.
"""

from enum import Enum

class ComponentType(Enum):
    """Types of system components."""
    CORE = "core"
    DATA = "data"
    GUI = "gui"
    INTEGRATION = "integration"
    ORGANIZER = "organizer"
    PROFESSIONAL = "professional"
    PUBLIC_SERVICE = "public_service"
    RAG = "rag"
    SERVICER = "servicer"
    STRUCTURE = "structure"
    UTILITY = "utility"

__all__ = ['ComponentType'] 