"""A retrieval projection importing only the stable harness API."""

from dataclasses import replace

from pi_agent_py.api import ContextPlan, Message, Middleware, ProviderRequestContext


class Rag:
    async def prepare(
        self, context: ProviderRequestContext, plan: ContextPlan
    ) -> ContextPlan:
        index = 0
        while index < len(plan.messages) and plan.messages[index].role == "system":
            index += 1
        injected = Message("system", "retrieved-reference")
        return replace(
            plan, messages=plan.messages[:index] + (injected,) + plan.messages[index:]
        )


def rag_middleware() -> Middleware:
    return Middleware("rag", provider_request=Rag())
