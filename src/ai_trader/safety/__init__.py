"""安全相关的基础设施：急停持久化。"""
from .halt import (
    HALT_FILENAME,
    RESUME_PHRASE,
    SOURCE_CONFIG,
    SOURCE_NONE,
    SOURCE_SENTINEL,
    SOURCE_STORAGE_ERROR,
    HaltState,
    HaltStore,
    HaltStoreError,
)

__all__ = [
    "HALT_FILENAME", "RESUME_PHRASE",
    "SOURCE_CONFIG", "SOURCE_NONE", "SOURCE_SENTINEL", "SOURCE_STORAGE_ERROR",
    "HaltState", "HaltStore", "HaltStoreError",
]
