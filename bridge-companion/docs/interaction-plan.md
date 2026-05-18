# Bridge Companion Interaction Plan

## Correction

The interaction layer must not feel like generic web hover effects or icons pasted on top of an image. The target is image-faithful scene dynamics: the generated contract-board image should feel alive while preserving its original shapes, composition, and authority boundaries.

## Interaction Principle

```text
Scene interaction is inspection, not control.
```

The user may inspect visible scene objects, but cannot control Bridge Runtime from this UI layer.

Allowed:

```text
hover/click to reveal factual explanation
local light reaction
parchment focus
medallion inspection
portal shimmer
candle light breathing
subtle lens/glint following cursor
```

Forbidden:

```text
start bridge
dispatch task
approve route
change phase
send agent instruction
invent progress
turn scene objects into control buttons
```

## Visual Strategy

Use the existing background image as the source of truth for visual geometry. Do not draw replacement flames or fake symbols. Instead, animate image regions and light fields aligned to the original objects.

Layer stack:

```text
1. static generated background image
2. global vignette and contrast correction
3. image-region clones for flame/portal/active medallion shimmer
4. light wash layers for candle and portal illumination
5. small particle/dust layer, extremely subtle
6. runtime UI text overlays
7. invisible inspection hotspots
8. tooltip/inspector glint
```

## Hotspots

### Main dossier / parchment

Purpose: explain the current runtime fact.

Interaction:

```text
hover: parchment brightens very slightly, edge glow appears
click: companion note says the main dossier only shows recorded runtime facts
no state mutation
```

### Portal / bridge gate

Purpose: explain bridge window / gateway metaphor.

Interaction:

```text
hover: portal shimmer intensity increases
click: note clarifies that this is not a control entrance
```

### Left candles

Purpose: ambient life and waiting explanation.

Interaction:

```text
hover: candle light breathes stronger, heat shimmer increases
click: note explains quiet foreground does not mean stuck
```

### Right lantern / desk props

Purpose: gateway/read status explanation.

Interaction:

```text
hover: right-bottom light wash increases
click: note explains gateway state is separate from runtime state
```

### Stage medallions

Purpose: inspect lifecycle phases.

Interaction:

```text
hover: medallion lifts, original-region glow strengthens, line glints
click: note explains that the phase is display-only and comes from lifecycle status
```

## Runtime Boundaries

Only two actions may read data:

```text
GET /runs/:runId/status
local mock render
```

All other interactions are local visual inspection.

## Implementation Plan

First, remove crude effect shapes. Replace them with image-region clones using the same background image. Each animated region should be masked to the original object area and use brightness/saturation/opacity/keyframe changes instead of drawing new flames.

Second, add a single scene state variable:

```text
idle | inspecting-dossier | inspecting-portal | inspecting-candles-left | inspecting-lantern-right | inspecting-stage
```

This state only affects CSS classes and companion note text.

Third, add pointer-based parallax at very low intensity:

```text
background/atmosphere translate less than 8px
cards translate less than 4px
no motion that damages text readability
```

Fourth, add refined tooltips and a small inspector glint. Tooltips should explain factual boundaries, not fantasy lore.

Fifth, verify by grep that no write/control endpoints or bridge calls exist.