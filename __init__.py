import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import Dict, List, Optional

from providers.base import BaseProvider

_REGISTRY: Dict[str, BaseProvider] = {}


def load_providers() -> Dict[str, BaseProvider]:
    """Auto-discover every BaseProvider subclass inside `providers/*.py`."""
    _REGISTRY.clear()
    pkg_dir = Path(__file__).parent
    for mod_info in pkgutil.iter_modules([str(pkg_dir)]):
        if mod_info.name.startswith("_") or mod_info.name == "base":
            continue
        module = importlib.import_module(f"providers.{mod_info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseProvider) and obj is not BaseProvider and obj.provider_type:
                _REGISTRY[obj.provider_type] = obj()
    return _REGISTRY


def get_provider(provider_type: str) -> Optional[BaseProvider]:
    if not _REGISTRY:
        load_providers()
    return _REGISTRY.get(provider_type)


def list_provider_schemas() -> List[Dict]:
    if not _REGISTRY:
        load_providers()
    return [p.schema() for p in _REGISTRY.values()]
