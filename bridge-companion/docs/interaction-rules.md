# Interaction Rules

Bridge Companion interactions are scene inspection only. They make the contract-board scene feel alive, but they do not control Bridge Runtime.

## Implemented Interactions

The prototype currently supports:

```text
image-region animation for original candle/lantern/portal/active medallion areas
local hover/click inspection hotspots
stage medallion hover/click inspection
cursor-following low-intensity light wash
small focus plaque with factual explanation
pinned inspection on click; Escape clears it
```

## Read-Only Guarantee

The only network read used by the prototype is:

```text
GET /runs/:runId/status
```

No scene hotspot calls an API. No interaction dispatches workflow events, calls bridge SDK, creates tasks, or mutates runtime.

## Copy Guarantee

Hotspot explanations must clarify boundaries. They should not invent progress.

Examples:

```text
桥门区域：只表示 bridge window / gateway 的视觉位置，不代表任务控制入口。
主卷宗只显示 runtime 已记录的事实，不补写内部进展。
网关断开时只代表无法读取状态，不代表任务失败。
```

## Visual Guarantee

The animation layer should preserve the generated image. It should use masked image-region clones and light fields, not replacement flame icons or unrelated decorative sprites.