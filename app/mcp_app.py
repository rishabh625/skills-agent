from __future__ import annotations

import uuid

from starlette.responses import JSONResponse

from fastmcp import FastMCP

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent
from app.skills_registry import (
    list_skill_names,
    load_all_skills_markdown,
    read_skill_markdown,
    search_skills,
)

mcp = FastMCP("skills-agent-skills")


def _run_agent(prompt: str) -> str:
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(user_id="mcp", app_name="app")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")

    message = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])

    events = list(
        runner.run(
            new_message=message,
            user_id="mcp",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )

    parts: list[str] = []
    for event in events:
        if not event.content or not event.content.parts:
            continue
        for part in event.content.parts:
            if part.text:
                parts.append(part.text)

    return "".join(parts).strip()


def _execute_skill(tool_name: str, request: str) -> str:
    skill_md = read_skill_markdown(tool_name)
    if not skill_md:
        raise ValueError(f"Unknown skill: {tool_name}")

    prompt = (
        f"Skill: {tool_name}\n"
        f"Invocation ID: {uuid.uuid4()}\n\n"
        "Follow the skill instructions exactly.\n\n"
        "Skill instructions:\n"
        f"{skill_md}\n\n"
        "User request:\n"
        f"{request}\n"
    )
    return _run_agent(prompt)


@mcp.tool()
def list_skills() -> list[str]:
    return list_skill_names()


@mcp.tool()
def get_skill(tool_name: str) -> str:
    content = read_skill_markdown(tool_name)
    if not content:
        raise ValueError(f"Unknown skill: {tool_name}")
    return content


@mcp.tool()
def search_skill_docs(query: str, max_results: int = 20) -> list[dict]:
    return search_skills(query, max_results=max_results)


@mcp.tool()
def get_all_skills() -> str:
    return load_all_skills_markdown()


@mcp.resource("skills://list")
def skills_list_resource() -> str:
    names = list_skill_names()
    return "\n".join(names)


@mcp.resource("skills://{tool_name}")
def skill_resource(tool_name: str) -> str:
    content = read_skill_markdown(tool_name)
    if not content:
        raise ValueError(f"Unknown skill: {tool_name}")
    return content


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request):
    return JSONResponse({"ok": True, "service": "skills-agent-mcp"})

_RESERVED_TOOL_NAMES = {
    "list_skills",
    "get_skill",
    "search_skill_docs",
    "get_all_skills",
}

for _tool_name in list_skill_names():
    if _tool_name in _RESERVED_TOOL_NAMES:
        continue

    def _make_tool(tool_name: str):
        def _tool(request: str) -> str:
            return _execute_skill(tool_name, request)

        _tool.__name__ = tool_name
        _tool.__annotations__ = {"request": str, "return": str}
        return mcp.tool(name=tool_name)(_tool)

    _make_tool(_tool_name)


app = mcp.http_app()
