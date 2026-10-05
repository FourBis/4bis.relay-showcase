import httpx
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


@pytest.mark.asyncio
async def test_real_model_requests_are_blocked_and_test_model_still_works():
    test_result = await Agent("test").run("respond")
    assert test_result.output

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o-mini",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client = AsyncOpenAI(api_key="sk-test-only", http_client=http_client)
    agent = Agent(OpenAIChatModel(
        "gpt-4o-mini", provider=OpenAIProvider(openai_client=client)
    ))
    try:
        with pytest.raises(RuntimeError, match="ALLOW_MODEL_REQUESTS"):
            await agent.run("respond")
        assert requests == []
    finally:
        await client.close()
