# Zero-Install Parent-Sibling Workflow

This workflow is intentionally self-contained in one `.claude` directory.

Expected layout:

```text
workspace-parent/
  .claude/
  your-repo/
    .mcp.json
```

Start Claude Code from inside `your-repo/`:

```powershell
cd C:\path\to\workspace-parent\your-repo
claude
```

No installer, user-level config, or higher-level folder is required. The repo under test needs only a small project `.mcp.json` pointer file.

Create `your-repo/.mcp.json` pointing to the sibling bridge server:

```json
{
  "mcpServers": {
    "bridge": {
      "type": "stdio",
      "command": "python",
      "args": ["../.claude/control/mcp/bridge_server.py"],
      "env": {
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1"
      }
    }
  }
}
```

If your Claude Code build does not auto-discover the sibling settings file, pass it explicitly:

```powershell
claude --settings ../.claude/settings.json
```

The `.claude/settings.json`, `.claude/mcp.json`, and project `.mcp.json` files contain:

- hook commands pointing to `../.claude/hooks/*.py`
- default front-facing agent `leader-orchestrator`
- the `bridge` MCP server pointing to `../.claude/control/mcp/bridge_server.py`

Runtime state is stored inside the same `.claude` tree, keyed by repo:

```text
workspace-parent/.claude/runtime_state/projects/<repo-key>/runs
```

Verification from this source package:

```powershell
python .claude/control/mcp/verify_bridge_mcp.py
python .claude/control/runtime/smoke_test.py
```
