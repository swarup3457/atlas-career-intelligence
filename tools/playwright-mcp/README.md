# Atlas Playwright MCP tool manifest

Pinned, reproducible install of the **Microsoft Playwright MCP** server
(`@playwright/mcp`, Apache-2.0) used by the Atlas Copilot-CLI browser backend
(`atlas/browser_backend/`).

## Contract

- Exact dependency: `@playwright/mcp@0.0.80` (no `@latest`, no range).
- `node_modules/` is **never committed** (see the repo `.gitignore` entry
  `tools/playwright-mcp/node_modules/`).
- The product install root defaults to
  `%LOCALAPPDATA%\Atlas\tools\playwright-mcp\0.0.80`.
- Installation is performed by the idempotent Python installer behind
  `atlas browser-backend install` (`atlas/browser_backend/install.py`).
  It requires Node >= 18, uses exact-lock installation (`npm ci` when the
  committed lockfile is present), never installs globally, never downloads a
  bundled browser (the installed Chrome/Edge channel is reused), and verifies
  the resolved version and license after install.

## Manual reproduction

```powershell
# Into the product install root (no global install):
npm ci --omit=dev
node ./node_modules/@playwright/mcp/cli.js --version   # -> Version 0.0.80
```

The launcher for an engineering canary may instead expose an already-installed
copy through `ATLAS_PLAYWRIGHT_MCP_CLI` / `ATLAS_PLAYWRIGHT_MCP_ROOT`; the
backend prefers those environment variables when present.
