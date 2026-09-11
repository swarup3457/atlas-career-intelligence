from __future__ import annotations

import re
from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).resolve().parents[1] / ".github" / "agents"
WORKER_AGENTS = [
    "atlas-company-search-vscode.agent.md",
    "atlas-company-correction-vscode.agent.md",
    "atlas-company-discovery-vscode.agent.md",
    "atlas-company-verification-vscode.agent.md",
]
CAMEL_BROWSER = [
    "openBrowserPage", "navigatePage", "readPage", "clickElement", "typeInPage",
    "handleDialog", "hoverElement", "dragElement", "screenshotPage", "runPlaywrightCode",
]
OBSOLETE_SNAKE = [
    "open_browser_page", "navigate_page", "read_page", "click_element", "type_in_page",
    "handle_dialog", "hover_element", "drag_element", "screenshot_page", "run_playwright_code",
]
PROHIBITED = [
    "run_in_terminal", "runInTerminal", "create_file", "replace_string_in_file",
    "multi_replace_string_in_file", "runCommands", "runTasks", "git", "terminal",
]


def _tools(agent_file: str) -> list[str]:
    text = (AGENTS_DIR / agent_file).read_text(encoding="utf-8")
    match = re.search(r"^tools:\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    assert match, f"{agent_file} has no explicit tools list"
    return [tool.strip() for tool in match.group(1).split(",") if tool.strip()]


@pytest.mark.parametrize("agent", WORKER_AGENTS)
def test_worker_uses_current_camelcase_browser_ids(agent: str) -> None:
    tools = _tools(agent)
    for tool_id in CAMEL_BROWSER:
        assert tool_id in tools, f"{agent} is missing camelCase Browser id {tool_id}"


@pytest.mark.parametrize("agent", WORKER_AGENTS)
def test_worker_has_no_obsolete_snakecase_browser_ids(agent: str) -> None:
    tools = _tools(agent)
    for tool_id in OBSOLETE_SNAKE:
        assert tool_id not in tools, f"{agent} still lists obsolete snake_case Browser id {tool_id}"


@pytest.mark.parametrize("agent", WORKER_AGENTS)
def test_worker_grants_atlas_runtime(agent: str) -> None:
    tools = _tools(agent)
    assert any(tool == "atlas-runtime" or tool.startswith("atlas-runtime/") for tool in tools), (
        f"{agent} must grant the atlas-runtime tool set"
    )


@pytest.mark.parametrize("agent", WORKER_AGENTS)
def test_worker_has_no_prohibited_tools(agent: str) -> None:
    tools = _tools(agent)
    for tool_id in PROHIBITED:
        assert tool_id not in tools, f"{agent} must not grant prohibited tool {tool_id}"
