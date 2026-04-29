# Zero-Install Parent-Sibling Workflow

This workflow is intentionally self-contained in one `.claude` directory.

Expected layout:

```text
workspace-parent/
  .claude/
  your-repo/
```

Start Claude Code from inside `your-repo/`:

```powershell
cd C:\path\to\workspace-parent\your-repo
claude
```

No installer, user-level config, workspace-level `.mcp.json`, or higher-level folder is required.

The `.claude/settings.json` file contains:

- hook commands pointing to `../.claude/hooks/*.py`
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
