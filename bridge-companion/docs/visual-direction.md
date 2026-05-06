# Visual Direction

## Correction

The first prototype was too information-dense and visually generic. Bridge Companion should not look like a normal dashboard. It should look like a dark fantasy contract board with very few, high-signal facts.

Reference direction:

```text
Witcher-style dark contract board
remote commission dossier
large current task title
single dominant status card
small top chips for run/phase/gateway
one short explanation
one short next-step note
one latest-event note
minimal companion notes
```

## Information Density Rule

The UI should not show every runtime field by default. Most users need only:

```text
当前任务
当前状态
一句事实说明
一句当前解读
一句下一步
最新事件
网关状态
1-2 条未知信息
```

Everything else belongs in a debug drawer or secondary inspector later.

## Visual Principles

Use:

```text
dark parchment
black metal borders
red-brown vertical banner
contract board composition
large fantasy title typography
ritual gate / bridge illustration
stage medallions
lantern/candle glow
muted gold text
low saturation background landscape
```

Avoid:

```text
generic SaaS cards
bright dashboard grids
too many badges
large tables
raw event timelines on the main page
technical metadata everywhere
```

## Copy Boundary Still Holds

Even when the UI says “委托” or “卷宗,” the main status sentence must remain factual.

Allowed decorative shell:

```text
Bridge Contract
远程委托卷宗
当前委托
同伴札记
```

Authoritative copy must still say:

```text
任务说明已下发，执行团队正在后台处理。runtime 暂未收到新的结构化报告或 artifact。
```

Do not write:

```text
队伍正在远端工坊中锻造核心结构
```

unless runtime reports that exact implementation fact in a structured report.

## Main Screen Layout

Recommended default screen:

```text
Top bar:
  Bridge Contract / 远程委托卷宗
  Run ID
  当前阶段
  网关状态

Hero:
  当前委托：<task title>
  one subtitle proving runtime source

Main card:
  visual bridge/portal icon
  current status title
  one factual status sentence

Phase line:
  冻结语义 -> 任务包 -> 桥接窗口 -> 等待回报 -> 完成检查 -> 返回结果

Side notes:
  当前解读
  下一步
  最新事件

Bottom companion notes:
  1-2 unknowns and one atmosphere note
```

## Debug Information

Detailed event logs, inbox entries, artifact refs, raw lifecycle state, and possible next events should not be on the main page. They can be added later behind:

```text
查看卷宗详情
Debug drawer
Runtime inspector
```

The first impression should be cinematic and calm, not operationally noisy.