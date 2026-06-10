import pytest

import server
from mcp.server.fastmcp.exceptions import ToolError


@pytest.mark.asyncio
async def test_lin_production_mcp_tool_exposure_hard_bans_experimental_tools():
    visible_required = {
        "breath",
        "hold",
        "grow",
        "read_bucket",
        "comment_bucket",
        "trace",
        "pulse",
        "introspection",
    }

    visible_tools = {tool.name for tool in await server.mcp.list_tools()}
    manager_visible_tools = {tool.name for tool in server.mcp._tool_manager.list_tools()}

    assert visible_required <= visible_tools
    assert server.HARD_HIDDEN_MCP_TOOLS.isdisjoint(visible_tools)
    assert visible_required <= manager_visible_tools
    assert server.HARD_HIDDEN_MCP_TOOLS.isdisjoint(manager_visible_tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(server.HARD_HIDDEN_MCP_TOOLS))
async def test_lin_production_hard_banned_mcp_tools_cannot_be_called(tool_name):
    with pytest.raises(ToolError, match="disabled in Lin production"):
        await server.mcp.call_tool(tool_name, {})

    with pytest.raises(ToolError, match="disabled in Lin production"):
        await server.mcp._tool_manager.call_tool(tool_name, {})
