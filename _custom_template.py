"""自定义反代插件模板

以后每当你发现一个新目标（比如原生 Kiro 或其他新网页接口）想直接集成进面板：
1. 复制本文件，改名为 `my_new_site.py`（注意去掉开头的下划线 `_`）。
2. 修改 `provider_type`、`label`、`fields` 以及请求逻辑。
3. 推送到 GitHub 并在 VPS 重启服务，面板里会自动出现你的新渠道！
"""

import json
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Tuple

from providers.base import BaseProvider, ProviderField, ProxyResult


class CustomSiteProvider(BaseProvider):
    provider_type = "custom_site_demo"
    label = "我的新反代模板"
    description = "复制并改名后即可把任意新协议直接接进总面板。"
    fields = [
        ProviderField(
            key="token",
            label="授权凭证 / RefreshToken",
            type="password",
            required=True,
            placeholder="粘贴你的凭证",
        ),
    ]

    async def list_models(self, config: Dict[str, Any]) -> List[str]:
        return ["custom-model-1"]

    async def test_connection(self, config: Dict[str, Any]) -> Tuple[bool, str]:
        if not config.get("token"):
            return False, "未填写凭证"
        return True, "模板测试通过（请替换为真实连通检测）"

    async def chat(self, config: Dict[str, Any], payload: Dict[str, Any]) -> ProxyResult:
        # TODO: 在这里调用目标接口，把返回文本拼成标准 OpenAI 格式
        reply_text = "这是自定义反代模板的测试回复。"
        body = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", "custom-model-1"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply_text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        return ProxyResult(status_code=200, body=body)

    async def chat_stream(
        self, config: Dict[str, Any], payload: Dict[str, Any]
    ) -> Tuple[int, AsyncIterator[bytes], Dict[str, Any]]:
        usage_info: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "error": ""}

        async def _gen() -> AsyncIterator[bytes]:
            chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": payload.get("model", "custom-model-1"),
                "choices": [{"index": 0, "delta": {"content": "模板流式测试回复。"}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

        return 200, _gen(), usage_info
