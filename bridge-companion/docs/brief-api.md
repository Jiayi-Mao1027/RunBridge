# Model Brief API

Bridge Companion supports an optional model-powered explanation layer. It is server-side only. The browser never receives the API key.

## Secret Storage

Do not hardcode the API key in project files. Do not put it in frontend JavaScript. Recommended local config file:

```bash
mkdir -p ~/.bridge-companion
cat > ~/.bridge-companion/brief-secret.json <<'JSON'
{
  "baseUrl": "https://api.deepseek.com",
  "apiKey": "YOUR_KEY_HERE",
  "model": "deepseek-v4-pro"
}
JSON
chmod 600 ~/.bridge-companion/brief-secret.json
```

You can also point to another file:

```bash
BRIDGE_BRIEF_SECRET_PATH=/secure/path/brief-secret.json node gateway/server.mjs
```

This keeps the key out of the repo, browser, command line arguments, and normal UI traffic. It is still readable by the same OS user running the gateway, so protect that account and file permissions.

## Endpoint

```text
POST /brief
```

Input:

```json
{
  "status": { "runtime facts here": true }
}
```

Output:

```json
{
  "configured": true,
  "model": "deepseek-v4-pro",
  "text": "..."
}
```

This endpoint does not mutate runtime. It only summarizes facts passed by the UI.

## Prompt Boundary

The gateway prompt instructs the model to:

```text
only summarize provided runtime facts
not invent file-level progress
not control Bridge Runtime
not send instructions to agents
explicitly preserve unknowns
```

## Why Not Obfuscation

Obfuscating a key in code is not encryption. If the application can recover the key at runtime, anyone with code/process access can recover it too. A permission-restricted local secret file is safer for this deployment style.