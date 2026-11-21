from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple


@dataclass
class ProviderField:
    key: str
    label: str
    type: str = "text"  # text | password | textarea | url
    required: bool = True
    placeholder: str = ""
    help: str = ""


@dataclass
class ProxyResult:
    """Standardized non-streaming chat completion response."""
    status_code: int
    body: Dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    error: str = ""


class BaseProvider(ABC):
    """Base class for all pluggable reverse-proxy providers.

    To add a brand-new reverse proxy (e.g. a new web service or custom protocol):
    1. Create a new `.py` file inside the `providers/` folder.
    2. Subclass `BaseProvider` and define `provider_type`, `label`, `description`, `fields`.
    3. Implement `chat()`, `chat_stream()`, `list_models()`, and `test_connection()`.
    4. Restart the service — the Web Panel will automatically detect your new provider!
    """

    provider_type: str = ""
    label: str = ""
    description: str = ""
    fields: List[ProviderField] = []

    def schema(self) -> Dict[str, Any]:
        return {
            "provider_type": self.provider_type,
            "label": self.label,
            "description": self.description,
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "type": f.type,
                    "required": f.required,
                    "placeholder": f.placeholder,
                    "help": f.help,
                }
                for f in self.fields
            ],
        }

    @abstractmethod
    async def list_models(self, config: Dict[str, Any]) -> List[str]:
        """Return available model IDs for this channel."""

    @abstractmethod
    async def test_connection(self, config: Dict[str, Any]) -> Tuple[bool, str]:
        """Return (ok, message) to verify credentials/connectivity from the Web Panel."""

    @abstractmethod
    async def chat(self, config: Dict[str, Any], payload: Dict[str, Any]) -> ProxyResult:
        """Handle a non-streaming OpenAI-format `/v1/chat/completions` request."""

    @abstractmethod
    async def chat_stream(
        self, config: Dict[str, Any], payload: Dict[str, Any]
    ) -> Tuple[int, AsyncIterator[bytes], Dict[str, Any]]:
        """Handle a streaming OpenAI-format `/v1/chat/completions` request.

        Returns `(status_code, async_byte_iterator, usage_collector_dict)`.
        `usage_collector_dict` may be mutated during streaming with keys:
        `prompt_tokens`, `completion_tokens`, `total_tokens`, `error`.
        """
