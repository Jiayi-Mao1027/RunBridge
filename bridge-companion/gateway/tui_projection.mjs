import { createHash } from "node:crypto";

const IMPORTANCE_THRESHOLD = 60;

const SOURCE_QUALITY_RANK = {
  unknown: 0,
  unbound: 1,
  assigned_only: 2,
  aggregate_summary: 3,
  report_only: 3,
  tool_activity: 4,
  live_text: 5
};

const TEAM_STATE_RANK = {
  unknown: 0,
  idle: 1,
  assigned: 2,
  reported: 3,
  waiting: 4,
  active: 5,
  blocked: 6,
  failed: 7
};

export function reduceToTuiView(events = [], snapshot = null, activeOperations = null, context = {}) {
  const orderedEvents = sourceDedupe(events);
  const currentBridgeWindowId = currentBridgeWindowIdFrom(snapshot, context, orderedEvents);
  const projectionContext = {
    ...context,
    currentBridgeWindowId
  };
  const header = buildHeader(orderedEvents, snapshot, projectionContext);
  const rawInspectorIndex = {};
  const displayItems = reduceDisplayItems(orderedEvents, {
    header,
    inspectorIndex: rawInspectorIndex
  });
  const teamEvents = currentBridgeWindowId
    ? orderedEvents.filter(event => eventBridgeWindowId(event) === currentBridgeWindowId)
    : orderedEvents;
  const teamTree = buildTeamTree(teamEvents, scopedActiveOperations(activeOperations, currentBridgeWindowId), projectionContext);
  const mainReport = buildMainReport(orderedEvents, snapshot, projectionContext);
  const completion = buildCompletionModel(orderedEvents, projectionContext);
  const waitingItem = buildWaitingItem(orderedEvents, header, displayItems);
  if (waitingItem) mergeDisplayItem(displayItems, waitingItem, null, rawInspectorIndex);

  const activityItems = [...displayItems.values()]
    .filter(item => item.importance >= IMPORTANCE_THRESHOLD && !item.anchored)
    .sort(compareRenderItems)
    .slice(0, 120);

  const inspectorIndex = {};
  for (const item of [...displayItems.values(), mainReport, completion, ...teamTree].filter(Boolean)) {
    if (!item.id) continue;
    inspectorIndex[item.id] = mergeInspectorPayload(
      rawInspectorIndex[item.id] || {},
      item.inspector || {},
      {
        id: item.id,
        displayKey: item.displayKey,
        kind: item.kind || item.type,
        title: item.title || item.teammateId || item.label,
        rawRefs: item.rawRefs || []
      }
    );
  }

  const unknowns = mergeUnknowns([
    ...unknownsForSources(orderedEvents, snapshot, activeOperations, projectionContext),
    ...(Array.isArray(projectionContext.unknowns) ? projectionContext.unknowns : [])
  ]);

  return {
    schemaVersion: "companion_tui_view.v1",
    generatedAt: new Date().toISOString(),
    header,
    mainReport,
    teamTree,
    activityItems,
    completion,
    unknowns,
    inspectorIndex,
    debugSummary: {
      rawEventCount: Array.isArray(events) ? events.length : 0,
      sourceDedupedEventCount: orderedEvents.length,
      displayItemCount: displayItems.size,
      visibleActivityItemCount: activityItems.length,
      hiddenLowImportanceItemCount: Math.max(0, displayItems.size - activityItems.length)
    }
  };
}

function sourceDedupe(events) {
  const byId = new Map();
  for (const event of Array.isArray(events) ? events : []) {
    if (!event || typeof event !== "object") continue;
    const id = event.eventId || rawRefKey(event.rawRef) || String(event.seq || stableHash(event));
    if (!byId.has(id)) byId.set(id, event);
  }
  return sortEvents([...byId.values()]);
}

function currentBridgeWindowIdFrom(snapshot, context, events) {
  const candidates = [
    context?.currentBridgeWindowId,
    context?.packetSummary?.packetSummary?.bridge_window_id,
    context?.packetSummary?.packetSummary?.bridgeWindowId,
    context?.packetSummary?.packet?.bridge_window_id,
    context?.packetSummary?.packet?.bridgeWindowId,
    snapshot?.last_bridge_result?.bridge_window_id,
    snapshot?.last_bridge_result?.bridgeWindowId,
    snapshot?.lifecycle?.current_bridge_window_id,
    snapshot?.lifecycle?.currentBridgeWindowId,
    snapshot?.lifecycle?.latest_bridge_window_id,
    snapshot?.lifecycle?.latestBridgeWindowId
  ];
  for (const value of candidates) {
    const id = stringId(value);
    if (id) return id;
  }
  const latestScopedEvent = [...events].reverse().find(event =>
    eventBridgeWindowId(event) &&
    (event.source === "bridge_packet" ||
      event.source === "completion_check" ||
      isBridgeResultReportEvent(event) ||
      event.kind === "lifecycle_transition")
  );
  return eventBridgeWindowId(latestScopedEvent);
}

function eventBridgeWindowId(event) {
  const raw = event?.raw && typeof event.raw === "object" ? event.raw : {};
  const payload = raw.payload && typeof raw.payload === "object" ? raw.payload : {};
  const result = payload.bridge_result || raw.bridge_result || raw.result?.bridge_result || {};
  const packet = raw.packet || payload.packet || {};
  return stringId(event?.bridgeWindowId) ||
    stringId(raw.bridge_window_id) ||
    stringId(raw.bridgeWindowId) ||
    stringId(payload.bridge_window_id) ||
    stringId(payload.bridgeWindowId) ||
    stringId(result.bridge_window_id) ||
    stringId(result.bridgeWindowId) ||
    stringId(packet.bridge_window_id) ||
    stringId(packet.bridgeWindowId);
}

function scopedActiveOperations(activeOperations, bridgeWindowId) {
  if (!bridgeWindowId || !activeOperations || typeof activeOperations !== "object") return activeOperations;
  if (!Array.isArray(activeOperations.teammates)) return activeOperations;
  return {
    ...activeOperations,
    teammates: activeOperations.teammates.filter(item =>
      !item?.bridge_window_id && !item?.bridgeWindowId
        ? true
        : stringId(item.bridge_window_id || item.bridgeWindowId) === bridgeWindowId
    )
  };
}

function stringId(value) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  return text || null;
}

function reduceDisplayItems(events, context) {
  const items = new Map();
  for (const event of events) {
    for (const item of renderItemsFromEvent(event)) {
      mergeDisplayItem(items, item, event, context.inspectorIndex);
    }
  }
  return items;
}

function renderItemsFromEvent(event) {
  const item = renderItemFromEvent(event);
  const items = item ? [item] : [];
  for (const summary of teammateToolUseSummaries(event)) {
    const displayKey = `teammate_tool_summary:${event.teamId || "team"}:${summary.teammateId}:${event.eventId}`;
    items.push({
      id: displayId("teammate_tool_summary", displayKey),
      displayKey,
      kind: "teammate_tool_summary",
      lane: "tools",
      title: `${summary.teammateId} tool-use summary`,
      body: `${summary.count} tool ${summary.count === 1 ? "use" : "uses"} observed in Claude transcript; individual tool names were not emitted to tool_events.jsonl.`,
      actor: summary.teammateId,
      status: "observed",
      observedToolUseCount: summary.count,
      importance: 72,
      lastTs: event.ts || null,
      rawRefs: rawRefs(event),
      evidenceRefs: evidenceRefs(event)
    });
  }
  return items;
}

function renderItemFromEvent(event) {
  const failure = isFailureLike(event);
  if (isLeaderReportEvent(event)) {
    return {
      id: displayId("leader_report", leaderReportKey(event)),
      displayKey: leaderReportKey(event),
      kind: "leader_report",
      lane: "reports",
      title: "Main leader report",
      body: leaderReportSummary(event),
      status: leaderReportStatus(event),
      actor: actorLabel(event),
      importance: 95,
      anchored: true,
      lastTs: event.ts || null,
      rawRefs: rawRefs(event),
      evidenceRefs: evidenceRefs(event)
    };
  }
  if (event.source === "hook_tool_event") {
    const toolId = toolDisplayKey(event);
    const status = toolStatus(event);
    return {
      id: displayId("tool", toolId),
      displayKey: toolId,
      kind: "tool",
      lane: "tools",
      title: `${actorLabel(event)}  ${event.toolName || "Tool"}`,
      body: toolBody(event),
      actor: actorLabel(event),
      toolName: event.toolName || "Tool",
      currentTarget: event.target || firstFileRef(event),
      status,
      durationMs: numberOrNull(event.raw?.duration_ms),
      startedAt: event.raw?.started_at || (event.kind === "tool_started" ? event.ts : null),
      completedAt: event.raw?.completed_at || (event.kind !== "tool_started" ? event.ts : null),
      fileRefs: event.fileRefs || [],
      evidenceRefs: evidenceRefs(event),
      importance: failure ? 95 : event.kind === "tool_started" ? 85 : hasUsefulFileRefs(event) ? 80 : 65,
      lastTs: event.ts || null,
      rawRefs: rawRefs(event)
    };
  }
  if (event.source === "agent_message") {
    const to = teammateTargetFromAssignment(event);
    const message = assignmentMessageBody(event, to);
    return {
      id: displayId("assignment", assignmentDisplayKey(event, to)),
      displayKey: assignmentDisplayKey(event, to),
      kind: "assignment",
      lane: "discussion",
      title: `${actorLabel(event)} -> ${to || "teammate"}`,
      body: compact(message || "assignment dispatched", 360),
      actor: to || actorLabel(event),
      from: event.raw?.from || event.raw?.agent_id || event.actor?.displayName || "bridge-leader",
      to: to || null,
      status: "assigned",
      importance: 75,
      lastTs: event.ts || null,
      rawRefs: rawRefs(event),
      evidenceRefs: evidenceRefs(event)
    };
  }
  if (event.source === "teammate_report") {
    const report = teammateReportSummary(event);
    return {
      id: displayId("report", report.displayKey),
      displayKey: report.displayKey,
      kind: "teammate_report",
      lane: "reports",
      title: `${report.teammateId} report`,
      body: report.summary,
      actor: report.teammateId,
      status: report.blockedCount > 0 ? "blocked" : "reported",
      completedCount: report.completedCount,
      openCount: report.openCount,
      blockedCount: report.blockedCount,
      evidenceCount: report.evidenceCount,
      importance: report.blockedCount > 0 ? 95 : 80,
      lastTs: event.ts || null,
      rawRefs: rawRefs(event),
      evidenceRefs: evidenceRefs(event)
    };
  }
  if (event.source === "artifact") {
    const key = artifactDisplayKey(event);
    return {
      id: displayId("artifact", key),
      displayKey: key,
      kind: "artifact",
      lane: "reports",
      title: "Artifact ready",
      body: compact(event.raw?.summary || event.raw?.safe_preview || event.messagePreview || artifactLabel(event), 260),
      actor: actorLabel(event),
      status: event.status || "recorded",
      artifactRef: event.raw?.artifact_ref || event.raw?.artifact_id || event.target || null,
      evidenceRefs: evidenceRefs(event),
      importance: 75,
      lastTs: event.ts || null,
      rawRefs: rawRefs(event)
    };
  }
  if (event.source === "completion_check") {
    const key = completionDisplayKey(event);
    return {
      id: displayId("completion", key),
      displayKey: key,
      kind: "completion_check",
      lane: "completion",
      title: "Completion contract changed",
      body: compact(event.raw?.summary || event.messagePreview || event.kind || "completion check recorded", 260),
      actor: "completion",
      status: completionStatus(event),
      importance: completionStatus(event) === "rejected" ? 95 : 70,
      lastTs: event.ts || null,
      rawRefs: rawRefs(event),
      evidenceRefs: evidenceRefs(event)
    };
  }
  if (event.source === "process_event") {
    const key = processDisplayKey(event);
    return {
      id: displayId("process", key),
      displayKey: key,
      kind: "process",
      lane: "processes",
      title: `${actorLabel(event)} process`,
      body: compact(event.raw?.command_preview || event.raw?.summary || event.messagePreview || "process activity", 260),
      actor: actorLabel(event),
      status: processStatus(event),
      importance: failure ? 95 : event.kind === "process_heartbeat" ? 40 : 70,
      lastTs: event.ts || null,
      rawRefs: rawRefs(event),
      evidenceRefs: evidenceRefs(event)
    };
  }
  if (event.source === "sdk_stream") {
    if (event.textDelta || (event.kind === "text_delta" && event.messagePreview)) {
      const key = textDisplayKey(event);
      return {
        id: displayId("text", key),
        displayKey: key,
        kind: "discussion",
        lane: "discussion",
        title: `${actorLabel(event)} says`,
        body: event.textDelta || event.messagePreview || "",
        actor: actorLabel(event),
        status: event.status || "streaming",
        text: event.textDelta || event.messagePreview || "",
        importance: 65,
        lastTs: event.ts || null,
        rawRefs: rawRefs(event),
        evidenceRefs: evidenceRefs(event)
      };
    }
    if (event.kind === "sdk_tool_declared" || event.kind === "sdk_tool_result" || event.toolInputDelta) {
      const key = sdkToolDisplayKey(event);
      return {
        id: displayId("sdk_tool", key),
        displayKey: key,
        kind: "sdk_tool_declaration",
        lane: "discussion",
        title: `SDK ${event.sdkToolName || event.toolName || "tool"}`,
        body: compact(event.toolInputDelta || event.messagePreview || "SDK tool declaration", 260),
        actor: actorLabel(event),
        status: event.status || "observed",
        importance: 30,
        lastTs: event.ts || null,
        rawRefs: rawRefs(event),
        evidenceRefs: evidenceRefs(event)
      };
    }
    return {
      id: displayId("metadata", metadataDisplayKey(event)),
      displayKey: metadataDisplayKey(event),
      kind: "metadata",
      lane: "debug",
      title: "SDK metadata",
      body: compact(event.messagePreview || event.streamEventType || event.kind || "SDK stream event", 180),
      actor: actorLabel(event),
      status: event.status || "observed",
      importance: isFailureLike(event) ? 95 : 0,
      lastTs: event.ts || null,
      rawRefs: rawRefs(event),
      evidenceRefs: evidenceRefs(event)
    };
  }
  if (event.kind === "lifecycle_transition" || event.source === "runtime_snapshot") {
    const key = lifecycleDisplayKey(event);
    return {
      id: displayId("lifecycle", key),
      displayKey: key,
      kind: "lifecycle",
      lane: "status",
      title: lifecycleTitle(event),
      body: compact(event.messagePreview || event.raw?.safe_preview || event.raw?.event_kind || event.raw?.to_status || "lifecycle transition", 220),
      actor: "runtime",
      status: event.raw?.to_status || event.status || "recorded",
      importance: failure ? 95 : isWaitingLifecycle(event) ? 45 : 35,
      lastTs: event.ts || null,
      rawRefs: rawRefs(event),
      evidenceRefs: evidenceRefs(event)
    };
  }
  if (failure) {
    const key = failureDisplayKey(event);
    return {
      id: displayId("failure", key),
      displayKey: key,
      kind: "failure",
      lane: "failures",
      title: "Runtime failure or blocker",
      body: compact(event.messagePreview || event.kind || event.status || "failure recorded", 260),
      actor: actorLabel(event),
      status: event.status || "failed",
      importance: 95,
      lastTs: event.ts || null,
      rawRefs: rawRefs(event),
      evidenceRefs: evidenceRefs(event)
    };
  }
  return null;
}

function mergeDisplayItem(items, item, event, inspectorIndex) {
  const key = item.displayKey || item.id;
  const existing = items.get(key);
  const merged = existing ? mergeRenderItem(existing, item) : normalizeRenderItem(item);
  items.set(key, merged);
  const id = merged.id;
  if (inspectorIndex && id) {
    inspectorIndex[id] = mergeInspectorPayload(inspectorIndex[id] || {}, {
      displayKey: key,
      rawRefs: item.rawRefs || rawRefs(event),
      normalizedEvents: event ? [event] : [],
      sourceCursors: event?.cursor ? [event.cursor] : [],
      evidenceRefs: item.evidenceRefs || (event ? evidenceRefs(event) : [])
    });
  }
}

function mergeRenderItem(previous, next) {
  const merged = normalizeRenderItem({ ...previous, ...next });
  merged.importance = Math.max(Number(previous.importance || 0), Number(next.importance || 0));
  merged.rawRefs = mergeArrayByKey(previous.rawRefs, next.rawRefs, rawRefKey);
  merged.evidenceRefs = mergePrimitiveArrays(previous.evidenceRefs, next.evidenceRefs);
  merged.fileRefs = mergePrimitiveArrays(previous.fileRefs, next.fileRefs);
  merged.lastTs = latestTimestamp(previous.lastTs, next.lastTs);
  if (previous.kind === "tool" || next.kind === "tool") {
    merged.kind = "tool";
    merged.lane = "tools";
    merged.startedAt = previous.startedAt || next.startedAt || null;
    merged.completedAt = next.completedAt || previous.completedAt || null;
    merged.durationMs = next.durationMs ?? previous.durationMs ?? null;
    merged.status = mergeToolStatus(previous.status, next.status);
    merged.body = toolBodyFromMerged(merged);
  }
  if (previous.kind === "discussion" && next.kind === "discussion") {
    merged.text = compact(`${previous.text || previous.body || ""}${next.text || next.body || ""}`, 4000);
    merged.body = merged.text;
  }
  return merged;
}

function normalizeRenderItem(item) {
  return {
    ...item,
    id: item.id || displayId("item", item.displayKey || item.title || stableHash(item)),
    displayKey: item.displayKey || item.id,
    rawRefs: Array.isArray(item.rawRefs) ? item.rawRefs.filter(Boolean) : [],
    evidenceRefs: Array.isArray(item.evidenceRefs) ? item.evidenceRefs.filter(Boolean) : [],
    fileRefs: Array.isArray(item.fileRefs) ? item.fileRefs.filter(Boolean) : []
  };
}

function buildHeader(events, snapshot, context) {
  const latest = [...events].reverse().find(event => importanceForLatest(event) >= IMPORTANCE_THRESHOLD) || events.at(-1) || null;
  const lifecycleState = latestLifecycleState(snapshot, events, context.currentBridgeWindowId);
  return {
    title: "RunBridge",
    repoKey: context.repoKey || snapshot?.repo_key || events.find(event => event.repoKey)?.repoKey || "unknown",
    runId: context.runId || snapshot?.run_id || events.find(event => event.runId)?.runId || "unknown",
    bridgeWindowId: context.currentBridgeWindowId || null,
    taskTitle: taskTitleFrom(context.packetSummary, snapshot),
    phase: snapshot?.current_phase || context.packetSummary?.targetPhase || "unknown",
    lifecycleState,
    latestMeaningfulEvent: latest ? compact(renderItemFromEvent(latest)?.body || latest.messagePreview || latest.kind || latest.source, 220) : "No meaningful event captured.",
    nextStep: nextStepForLifecycle(lifecycleState),
    updatedAt: latest?.ts || snapshot?.updated_at || null
  };
}

function buildMainReport(events, snapshot, context) {
  const latestReport = [...events].reverse().find(isBridgeResultReportEvent)
    || [...events].reverse().find(isLeaderReportEvent);
  const snapshotResult = snapshot?.last_bridge_result && typeof snapshot.last_bridge_result === "object"
    ? snapshot.last_bridge_result
    : null;
  const rawRefsValue = latestReport ? rawRefs(latestReport) : [];
  const status = latestReport ? leaderReportStatus(latestReport) : snapshotResult?.status || "not_returned";
  const summary = latestReport
    ? leaderReportSummary(latestReport)
    : snapshotResult?.reports_preview?.[0]?.summary || snapshotResult?.error_or_null?.message || statusNarrative(context.packetSummary, snapshot);
  const key = latestReport ? leaderReportKey(latestReport) : `main_report:${context.runId || snapshot?.run_id || "unknown"}`;
  return {
    id: displayId("main_report", key),
    displayKey: key,
    kind: "main_report",
    title: "Main Leader Report",
    status,
    handledBy: latestReport ? leaderReportHandledBy(latestReport) : "runtime snapshot",
    summary: compact(summary, 4000),
    body: compact(summary, 4000),
    task: taskTitleFrom(context.packetSummary, snapshot),
    currentState: latestLifecycleState(snapshot, events, context.currentBridgeWindowId),
    nextStep: nextStepForLifecycle(latestLifecycleState(snapshot, events, context.currentBridgeWindowId)),
    rawRefs: rawRefsValue,
    evidenceRefs: latestReport ? evidenceRefs(latestReport) : [],
    inspector: {
      normalizedEvents: latestReport ? [latestReport] : [],
      rawRefs: rawRefsValue,
      snapshotRefs: snapshot?.snapshot_refs || {}
    }
  };
}

function buildTeamTree(events, activeOperations, context) {
  const members = new Map();
  const ensure = (id, seed = {}) => {
    const key = String(id || seed.teammateId || seed.role || seed.sessionId || "unknown");
    if (!members.has(key)) {
      members.set(key, {
        id: displayId("teammate", key),
        displayKey: `teammate:${key}`,
        teammateId: seed.teammateId || key,
        role: seed.role || seed.agentType || key,
        state: "unknown",
        currentAction: "unknown",
        currentTool: null,
        currentTarget: null,
        lastCompletedTool: null,
        lastReportSummary: null,
        lastTextPreview: null,
        sourceQuality: "unknown",
        unknowns: [],
        rawRefs: [],
        inspector: { rawRefs: [], normalizedEvents: [], sourceCursors: [] }
      });
    }
    const member = members.get(key);
    assignDefined(member, seed);
    return member;
  };

  ensure("bridge-leader", {
    teammateId: "bridge-leader",
    role: "bridge-leader",
    state: "waiting",
    currentAction: "watching runtime",
    sourceQuality: "unknown"
  });

  seedTeamFromPacket(events, ensure);
  seedTeamFromBindings(context.sessionBindings, ensure, context.currentBridgeWindowId);
  seedTeamFromActiveOperations(activeOperations, ensure);

  for (const event of events) {
    if (event.source === "agent_message") {
      const target = teammateTargetFromAssignment(event);
      const member = ensure(target || actorKey(event), { teammateId: target || actorKey(event), role: target || "teammate" });
      updateMemberState(member, "assigned");
      member.currentAction = compact(assignmentMessageBody(event, target) || "assigned", 130);
      pushQuality(member, "assigned_only");
      pushRaw(member, event);
      continue;
    }
    for (const summary of teammateToolUseSummaries(event)) {
      const member = ensure(summary.teammateId, { teammateId: summary.teammateId, role: summary.role || summary.teammateId });
      if (member.state === "unknown") updateMemberState(member, "idle");
      member.observedToolUseCount = summary.count;
      member.lastCompletedTool = {
        toolName: "tool uses",
        target: `${summary.count} observed`,
        status: "observed"
      };
      member.toolDetailAvailability = "aggregate_only";
      pushQuality(member, "aggregate_summary");
      pushRaw(member, event);
    }
    if (event.source === "hook_tool_event") {
      const key = actorKey(event);
      const member = ensure(key, {
        teammateId: event.actor?.teammateId || key,
        role: event.actor?.role || key
      });
      const tool = toolSummaryForTeam(event);
      if (event.kind === "tool_started") {
        updateMemberState(member, "active");
        member.currentAction = `${tool.toolName}${tool.target ? ` ${tool.target}` : ""}`;
        member.currentTool = tool.toolName;
        member.currentTarget = tool.target || null;
        member.currentToolUseId = tool.toolUseId;
      } else {
        member.state = event.kind === "tool_failed" ? "failed" : "idle";
        member.lastCompletedTool = tool;
        if (!member.currentTool || tool.toolUseId === member.currentToolUseId) {
          member.currentTool = null;
          member.currentTarget = null;
          member.currentAction = event.kind === "tool_failed" ? "tool failed" : "waiting";
        }
      }
      pushQuality(member, "tool_activity");
      pushRaw(member, event);
      continue;
    }
    if (event.source === "process_event") {
      const member = ensure(actorKey(event), { teammateId: event.actor?.teammateId || actorKey(event), role: event.actor?.role || actorKey(event) });
      updateMemberState(member, processStatus(event) === "running" ? "active" : "waiting");
      member.currentAction = compact(event.raw?.command_preview || event.messagePreview || "process running", 120);
      pushQuality(member, "tool_activity");
      pushRaw(member, event);
      continue;
    }
    if (event.source === "teammate_report") {
      const report = teammateReportSummary(event);
      const member = ensure(report.teammateId, { teammateId: report.teammateId, role: report.teammateId });
      updateMemberState(member, report.blockedCount > 0 ? "blocked" : "reported");
      member.lastReportSummary = report.summary;
      pushQuality(member, "report_only");
      pushRaw(member, event);
      continue;
    }
    if (event.source === "sdk_stream" && (event.textDelta || event.kind === "text_delta")) {
      const streamMemberId = hasTeammateAttribution(event) ? actorKey(event) : "bridge-leader";
      const member = ensure(streamMemberId, {
        teammateId: hasTeammateAttribution(event) ? event.actor?.teammateId || streamMemberId : "bridge-leader",
        role: hasTeammateAttribution(event) ? event.actor?.role || streamMemberId : "bridge-leader"
      });
      member.lastTextPreview = compact(event.textDelta || event.messagePreview || "", 180);
      if (member.state === "unknown") updateMemberState(member, "waiting");
      pushQuality(member, "live_text");
      pushRaw(member, event);
      continue;
    }
  }

  for (const member of members.values()) {
    if (member.sourceQuality === "tool_activity" && !member.lastTextPreview) {
      member.unknowns.push("teammate live text not captured");
    }
    if (member.sourceQuality === "assigned_only") {
      member.unknowns.push("no tool, text, or report evidence yet");
    }
    if (member.toolDetailAvailability === "aggregate_only") {
      member.unknowns.push("individual tool names not captured; only aggregate tool-use count observed");
    }
    member.rawRefs = mergeArrayByKey(member.rawRefs, member.inspector.rawRefs, rawRefKey);
    member.unknowns = mergePrimitiveArrays(member.unknowns, []);
  }

  return [...members.values()].sort((a, b) => {
    if (a.teammateId === "bridge-leader") return -1;
    if (b.teammateId === "bridge-leader") return 1;
    return String(a.teammateId).localeCompare(String(b.teammateId));
  });
}

function seedTeamFromPacket(events, ensure) {
  for (const event of events) {
    if (event.source !== "bridge_packet") continue;
    const team = Array.isArray(event.raw?.team_spec) ? event.raw.team_spec : [];
    for (const teammate of team) {
      if (!teammate || typeof teammate !== "object") continue;
      const id = teammate.teammate_id || teammate.teammate_name || teammate.agent_type || teammate.role;
      if (!id) continue;
      const member = ensure(id, {
        teammateId: id,
        role: teammate.role || teammate.agent_type || id,
        state: "assigned",
        currentAction: "assigned",
        sourceQuality: "assigned_only"
      });
      pushRaw(member, event);
    }
  }
}

function seedTeamFromBindings(bindings, ensure, bridgeWindowId = null) {
  for (const binding of Array.isArray(bindings) ? bindings : []) {
    if (!binding || typeof binding !== "object") continue;
    if (bridgeWindowId && stringId(binding.bridge_window_id || binding.bridgeWindowId) !== bridgeWindowId) continue;
    const raw = binding.rawRef ? { rawRef: binding.rawRef, eventId: rawRefKey(binding.rawRef), cursor: binding.rawRef } : null;
    const id = binding.teammate_id || binding.agent_type || binding.agent_id || binding.session_id;
    const sourceQuality = binding.run_binding_state === "unbound" ? "unbound" : "assigned_only";
    const member = ensure(id, {
      teammateId: binding.teammate_id || id,
      role: binding.agent_type || binding.teammate_id || "session",
      sessionId: binding.session_id,
      state: "assigned",
      currentAction: "bound session",
      sourceQuality
    });
    if (raw) pushRaw(member, raw);
  }
}

function seedTeamFromActiveOperations(activeOperations, ensure) {
  const teammates = Array.isArray(activeOperations?.teammates) ? activeOperations.teammates : [];
  for (const item of teammates) {
    const id = item.teammate_id || item.agent_id || item.agent_type || item.session_id;
    const member = ensure(id, {
      teammateId: item.teammate_id || id,
      role: item.agent_type || item.teammate_id || "session",
      sessionId: item.session_id,
      sourceQuality: "tool_activity"
    });
    if (item.active_tool) {
      updateMemberState(member, "active");
      member.currentTool = item.active_tool.tool_name || item.active_tool.toolName || null;
      member.currentToolUseId = item.active_tool.tool_use_id || item.active_tool.toolUseId || null;
      member.currentTarget = item.active_tool.target || null;
      member.currentAction = `${member.currentTool || "tool"}${member.currentTarget ? ` ${member.currentTarget}` : ""}`;
    }
    if (item.last_completed_tool) {
      member.lastCompletedTool = {
        toolName: item.last_completed_tool.tool_name || item.last_completed_tool.toolName || "tool",
        target: item.last_completed_tool.target || null,
        status: item.last_completed_tool.status || "completed"
      };
      if (member.state === "unknown") updateMemberState(member, "idle");
    }
    member.inspector.rawRefs.push({ sourceFile: "active_operations.json", sourceOffset: 1, sourceKind: "json" });
  }
}

function buildCompletionModel(events, context) {
  const completionEvents = events.filter(event => event.source === "completion_check");
  const latest = completionEvents.at(-1);
  const raw = latest?.raw || {};
  const checks = raw.completion_checks && typeof raw.completion_checks === "object" ? raw.completion_checks : {};
  const items = [];
  for (const item of Array.isArray(checks.checks) ? checks.checks : []) {
    if (!item || typeof item !== "object") continue;
    items.push({
      id: String(item.id || item.name || stableHash(item)),
      label: String(item.name || item.subject || "completion item"),
      status: normalizeChecklistStatus(item.status),
      message: compact(item.message || item.reason || "", 180),
      evidenceRefs: item.evidence_ref ? [item.evidence_ref] : []
    });
  }
  if (!items.length && Array.isArray(raw.items)) {
    for (const item of raw.items) {
      if (!item || typeof item !== "object") continue;
      items.push({
        id: String(item.id || item.name || stableHash(item)),
        label: String(item.text || item.name || "completion item"),
        status: normalizeChecklistStatus(item.status),
        message: compact(item.reason || item.message || "", 180),
        evidenceRefs: Array.isArray(item.evidence_refs) ? item.evidence_refs : []
      });
    }
  }
  if (!items.length) {
    const contract = context.packetSummary?.packet?.completion_contract || context.packetSummary?.packetSummary?.completion_contract || {};
    for (const label of [
      ...(Array.isArray(contract.required_outputs) ? contract.required_outputs : []),
      ...(Array.isArray(contract.required_artifacts) ? contract.required_artifacts : []),
      ...(Array.isArray(contract.validation_requirements) ? contract.validation_requirements : [])
    ]) {
      items.push({
        id: stableHash(label),
        label: String(label),
        status: "unknown",
        message: "not checked yet",
        evidenceRefs: []
      });
    }
  }
  const key = latest ? completionDisplayKey(latest) : `completion:${context.runId || "unknown"}`;
  return {
    id: displayId("completion_model", key),
    displayKey: key,
    kind: "completion",
    status: latest ? completionStatus(latest) : "unknown",
    finalDisposition: checks.final_disposition || undefined,
    validatedBy: checks.validated_by || undefined,
    items,
    rawRefs: latest ? rawRefs(latest) : [],
    inspector: {
      rawRefs: latest ? rawRefs(latest) : [],
      normalizedEvents: latest ? [latest] : []
    }
  };
}

function buildWaitingItem(events, header, displayItems) {
  if (!isWaitingState(header.lifecycleState)) return null;
  const last = [...displayItems.values()]
    .filter(item => item.importance >= IMPORTANCE_THRESHOLD && item.kind !== "metadata")
    .sort(compareRenderItems)[0];
  const key = `waiting:${header.runId}:${header.lifecycleState}`;
  return {
    id: displayId("waiting", key),
    displayKey: key,
    kind: "waiting",
    lane: "status",
    title: "Waiting for teammate evidence",
    body: `Last meaningful event: ${last?.title || header.latestMeaningfulEvent || "none captured"}`,
    actor: "runtime",
    status: "waiting",
    importance: 65,
    lastTs: header.updatedAt,
    rawRefs: []
  };
}

function unknownsForSources(events, snapshot, activeOperations, context) {
  const hasRunBinding = Array.isArray(context.sessionBindings) && context.sessionBindings.some(item => item?.teammate_id || item?.agent_type);
  const hasHookTool = events.some(event => event.source === "hook_tool_event");
  const hasTeammateTool = events.some(event => event.source === "hook_tool_event" && hasTeammateAttribution(event));
  const hasAggregateToolSummary = events.some(event => teammateToolUseSummaries(event).length > 0);
  const hasReport = events.some(event => event.source === "teammate_report");
  const hasCompletion = events.some(event => event.source === "completion_check");
  const hasSdkText = events.some(event => event.source === "sdk_stream" && (event.textDelta || event.kind === "text_delta"));
  const hasTeammateSdkText = events.some(event => event.source === "sdk_stream" && (event.textDelta || event.kind === "text_delta") && hasTeammateAttribution(event));
  const hasActiveOps = Array.isArray(activeOperations?.teammates) && activeOperations.teammates.length > 0;
  const unknowns = [];
  if (!snapshot) unknowns.push("runtime_snapshot missing.");
  if (!hasRunBinding) unknowns.push("No run-scoped teammate session binding captured.");
  if (!hasHookTool && !hasActiveOps) unknowns.push("No hook tool event captured; real tool cards are unavailable.");
  if (hasAggregateToolSummary && !hasTeammateTool) {
    unknowns.push("Claude transcript exposed only aggregate teammate tool-use counts; individual child tool names were not captured.");
  }
  if (hasTeammateTool && !hasTeammateSdkText) {
    unknowns.push("teammate tool activity captured; teammate live text not captured.");
  } else if (hasSdkText && !hasTeammateSdkText) {
    unknowns.push("SDK text is attributed to bridge-leader or an unknown source only; no child/subagent text attribution captured.");
  }
  if (!hasReport) unknowns.push("No structured teammate report captured.");
  if (!hasCompletion) unknowns.push("No completion check captured.");
  return unknowns;
}

function mergeUnknowns(values) {
  const canonical = new Map();
  for (const value of mergePrimitiveArrays(values.filter(Boolean).map(String), [])) {
    const key = unknownKey(value);
    if (!canonical.has(key) || String(value).length > String(canonical.get(key)).length) {
      canonical.set(key, value);
    }
  }
  return [...canonical.values()].slice(0, 20);
}

function unknownKey(value) {
  const text = String(value || "").toLowerCase();
  if (text.includes("runtime_snapshot")) return "runtime_snapshot";
  if (text.includes("session binding")) return "session_binding";
  if (text.includes("hook tool event") || text.includes("real tool cards")) return "hook_tool";
  if (text.includes("teammate report")) return "teammate_report";
  if (text.includes("completion check")) return "completion_check";
  if (text.includes("teammate live text") || text.includes("child/subagent text")) return "teammate_live_text";
  if (text.includes("discussion text")) return "discussion_text";
  return text;
}

function isLeaderReportEvent(event) {
  if (!event) return false;
  if (isBridgeResultReportEvent(event)) return true;
  if (event.source === "outer_host" && event.raw?.event_kind === "outer_leader_result") return true;
  if (event.source !== "sdk_stream") return false;
  const type = String(event.raw?.sdk_message_type || event.raw?.event_type || "");
  if (type === "ResultMessage" || type === "sdk_stream_final_result") return true;
  return event.raw?.raw_stream_event_type === "result" && Boolean(event.raw?.result || event.messagePreview);
}

function isBridgeResultReportEvent(event) {
  const raw = event?.raw || {};
  return Boolean(
    raw.payload?.bridge_result
    || raw.bridge_result
    || raw.result?.bridge_result
  );
}

function leaderReportKey(event) {
  const status = leaderReportStatus(event);
  const summary = leaderReportSummary(event);
  const bridgeWindow = event.bridgeWindowId || event.raw?.bridge_window_id || "run";
  return `bridge_result:${bridgeWindow}:${stableHash(`${status}|${summary}`)}`;
}

function leaderReportStatus(event) {
  const bridge = bridgeResultFromEvent(event);
  if (bridge) return bridge.status || event.raw?.status || event.status || "unknown";
  const payload = event.raw?.payload && typeof event.raw.payload === "object" ? event.raw.payload : {};
  const leader = payload.leader_result && typeof payload.leader_result === "object" ? payload.leader_result : {};
  return leader.status || event.raw?.status || event.status || "unknown";
}

function leaderReportHandledBy(event) {
  if (bridgeResultFromEvent(event)) return event.raw?.agent_id || event.raw?.agent_type || "main-leader";
  const payload = event.raw?.payload && typeof event.raw.payload === "object" ? event.raw.payload : {};
  const leader = payload.leader_result && typeof payload.leader_result === "object" ? payload.leader_result : {};
  return leader.handled_by || event.raw?.handled_by || event.raw?.source || "leader-orchestrator";
}

function leaderReportSummary(event) {
  const bridge = bridgeResultFromEvent(event);
  if (bridge) {
    const reports = Array.isArray(bridge.reports) ? bridge.reports : [];
    const reportLines = reports
      .map((report, index) => {
        const name = report?.teammate_name || report?.agent_type || report?.role || `report ${index + 1}`;
        return `${name}: ${report?.summary || "report recorded"}`;
      })
      .filter(Boolean);
    const header = [
      `BridgeResult status=${bridge.status || "unknown"}`,
      `report_count=${reports.length}`
    ].join("; ");
    return compact([header, ...reportLines].join("\n\n"), 4000);
  }
  const raw = event.raw || {};
  const payload = raw.payload && typeof raw.payload === "object" ? raw.payload : {};
  const leader = payload.leader_result && typeof payload.leader_result === "object" ? payload.leader_result : {};
  const report = Array.isArray(leader.reports) ? leader.reports[0] : null;
  return compact(report?.summary || raw.result || event.messagePreview || leader.error_or_null?.message || "leader result recorded", 4000);
}

function bridgeResultFromEvent(event) {
  const raw = event?.raw || {};
  const payload = raw.payload && typeof raw.payload === "object" ? raw.payload : {};
  if (payload.bridge_result && typeof payload.bridge_result === "object") return payload.bridge_result;
  if (raw.bridge_result && typeof raw.bridge_result === "object") return raw.bridge_result;
  if (raw.result?.bridge_result && typeof raw.result.bridge_result === "object") return raw.result.bridge_result;
  return null;
}

function taskTitleFrom(packetSummary, snapshot) {
  return packetSummary?.objective || snapshot?.semantic?.frozen?.task_subject || snapshot?.semantic?.frozen?.user_instruction || "No task packet recorded";
}

function latestLifecycleState(snapshot, events, bridgeWindowId = null) {
  const lifecycle = snapshot?.lifecycle || {};
  const open = Array.isArray(lifecycle.open_bridge_window_ids) ? lifecycle.open_bridge_window_ids[0] : null;
  const statusIndex = lifecycle.status_index && typeof lifecycle.status_index === "object" ? lifecycle.status_index : {};
  if (bridgeWindowId && statusIndex[bridgeWindowId]) return statusIndex[bridgeWindowId];
  if (open && statusIndex[open]) return statusIndex[open];
  const latestTransition = [...events].reverse().find(event =>
    event.kind === "lifecycle_transition"
    || event.raw?.event_kind === "lifecycle_transition"
    || event.raw?.kind === "lifecycle_transition"
  );
  const raw = latestTransition?.raw && typeof latestTransition.raw === "object" ? latestTransition.raw : {};
  const payload = raw.payload && typeof raw.payload === "object" ? raw.payload : {};
  const message = typeof latestTransition?.messagePreview === "string" && !latestTransition.messagePreview.trim().startsWith("{")
    ? latestTransition.messagePreview
    : "";
  return raw.to_status || payload.to_status || raw.status || payload.status || message || snapshot?.run_status || "unknown";
}

function nextStepForLifecycle(state) {
  const value = String(state || "").toLowerCase();
  if (value.includes("waiting") || value === "message_dispatch_completed") {
    return "Wait for teammate report, artifact, completion check, failure, or timeout evidence.";
  }
  if (value.includes("rejected")) return "Collect missing completion evidence or route to repair.";
  if (value.includes("failed")) return "Inspect failure evidence before retry or reroute.";
  if (value.includes("returned") || value.includes("completed")) return "Review returned result and completion evidence.";
  if (value.includes("opened") || value.includes("accepted")) return "Wait for team creation, task dispatch, or bridge result.";
  return "Follow the next legal lifecycle transition recorded by runtime.";
}

function statusNarrative(packetSummary, snapshot) {
  const state = latestLifecycleState(snapshot, []);
  return [
    `Current task: ${taskTitleFrom(packetSummary, snapshot)}`,
    `Current state: ${state}`,
    nextStepForLifecycle(state)
  ].join("\n");
}

function toolDisplayKey(event) {
  const id = event.toolId || event.raw?.tool_use_id;
  if (id) return `tool:${id}`;
  const started = event.raw?.started_at || event.raw?.source_event_id || event.ts || event.eventId;
  return `tool:${actorKey(event)}:${event.toolName || "tool"}:${event.target || firstFileRef(event) || "target"}:${started}`;
}

function sdkToolDisplayKey(event) {
  return `sdk_tool:${event.toolId || event.raw?.tool_id || event.sdkToolName || event.toolName || "unknown"}:${event.sessionId || "session"}`;
}

function textDisplayKey(event) {
  const block = event.raw?.content_block_id || event.raw?.content_block_index || event.raw?.index || "block";
  return `text:${event.sessionId || "session"}:${actorKey(event)}:${block}`;
}

function assignmentDisplayKey(event, to) {
  return `assignment:${event.teamId || "team"}:${event.taskId || "task"}:${to || actorKey(event)}`;
}

function teammateReportSummary(event) {
  const raw = event.raw || {};
  const report = raw.report && typeof raw.report === "object" ? raw.report : raw;
  const teammateId = raw.teammate_id || event.actor?.teammateId || event.actor?.displayName || event.actor?.role || "teammate";
  const completed = Array.isArray(raw.completed_items) ? raw.completed_items : Array.isArray(report.completed_items) ? report.completed_items : [];
  const open = Array.isArray(raw.open_items) ? raw.open_items : Array.isArray(report.open_items) ? report.open_items : [];
  const blocked = Array.isArray(raw.blocked_items) ? raw.blocked_items : Array.isArray(report.blocked_items) ? report.blocked_items : [];
  const evidence = Array.isArray(raw.evidence_refs) ? raw.evidence_refs : Array.isArray(report.evidence_refs) ? report.evidence_refs : [];
  const summary = compact(raw.summary || report.summary || event.messagePreview || "report received", 320);
  return {
    teammateId,
    summary,
    completedCount: completed.length,
    openCount: open.length,
    blockedCount: blocked.length,
    evidenceCount: evidence.length,
    displayKey: `report:${teammateId}:${raw.report_id || raw.source_event_id || stableHash(`${summary}|${evidence.join("|")}`)}`
  };
}

function artifactDisplayKey(event) {
  const raw = event.raw || {};
  const ref = raw.sha256 || raw.artifact_id || raw.path || raw.artifact_ref || event.target || event.messagePreview || event.eventId;
  return `artifact:${stableHash(ref)}`;
}

function completionDisplayKey(event) {
  const raw = event.raw || {};
  const item = raw.checklist_item_id || raw.check_type || raw.completion_checks?.validated_by || raw.status || event.kind;
  return `completion:${event.taskId || "task"}:${item}`;
}

function processDisplayKey(event) {
  const raw = event.raw || {};
  return `process:${raw.process_ref || raw.pid || raw.id || event.eventId}`;
}

function lifecycleDisplayKey(event) {
  const raw = event.raw || {};
  return `lifecycle:${event.bridgeWindowId || raw.bridge_window_id || "run"}:${raw.from_status || "null"}->${raw.to_status || raw.event_kind || event.kind}`;
}

function metadataDisplayKey(event) {
  return `metadata:${event.rawRef?.sourceFile || event.source}:${event.rawRef?.sourceOffset || event.eventId}`;
}

function failureDisplayKey(event) {
  return `failure:${event.bridgeWindowId || event.runId || "run"}:${event.kind || event.status}:${stableHash(event.messagePreview || event.eventId)}`;
}

function leaderResultEvidence(event) {
  const payload = event.raw?.payload && typeof event.raw.payload === "object" ? event.raw.payload : {};
  const leader = payload.leader_result && typeof payload.leader_result === "object" ? payload.leader_result : {};
  return leader.evidence || event.raw?.evidence || null;
}

function completionStatus(event) {
  const value = String(event.raw?.status || event.status || event.kind || "").toLowerCase();
  if (value.includes("reject") || value.includes("fail") || value.includes("blocked")) return "rejected";
  if (value.includes("satisf") || value.includes("pass") || value.includes("complete")) return "satisfied";
  return value || "unknown";
}

function normalizeChecklistStatus(value) {
  const text = String(value || "").toLowerCase();
  if (["pass", "passed", "satisfied", "completed", "done", "ok", "true"].includes(text)) return "passed";
  if (["fail", "failed", "rejected", "missing", "false"].includes(text)) return "failed";
  if (["blocked", "deferred"].includes(text)) return text;
  return text || "unknown";
}

function toolStatus(event) {
  if (event.kind === "tool_started") return "running";
  if (event.kind === "tool_failed" || event.status === "failed") return "failed";
  return "completed";
}

function mergeToolStatus(previous, next) {
  const values = [previous, next].map(value => String(value || "").toLowerCase());
  if (values.includes("failed")) return "failed";
  if (values.includes("completed")) return "completed";
  if (values.includes("running")) return "running";
  return next || previous || "unknown";
}

function toolBody(event) {
  const target = event.target || firstFileRef(event);
  const parts = [event.toolName || "Tool", target || "", event.status && event.kind !== "tool_started" ? event.status : ""].filter(Boolean);
  return compact(parts.join(" "), 260);
}

function toolBodyFromMerged(item) {
  const target = item.currentTarget || firstOf(item.fileRefs) || "";
  const status = item.status ? `status: ${item.status}` : "";
  return compact([item.toolName || "Tool", target, status].filter(Boolean).join("\n"), 320);
}

function toolSummaryForTeam(event) {
  return {
    toolUseId: event.toolId || event.raw?.tool_use_id || null,
    toolName: event.toolName || "Tool",
    target: event.target || firstFileRef(event) || null,
    status: toolStatus(event),
    durationMs: numberOrNull(event.raw?.duration_ms),
    rawRefs: rawRefs(event)
  };
}

function processStatus(event) {
  const value = String(event.raw?.state || event.status || event.kind || "").toLowerCase();
  if (value.includes("fail")) return "failed";
  if (value.includes("complete") || value.includes("exit") || value.includes("terminal")) return "completed";
  if (value.includes("running") || value.includes("heartbeat") || value.includes("poll")) return "running";
  return value || "unknown";
}

function teammateTargetFromAssignment(event) {
  return (
    event.raw?.to ||
    event.raw?.target_teammate_id ||
    event.raw?.teammate_id ||
    assignmentPrefix(event) ||
    event.actor?.teammateId ||
    null
  );
}

function assignmentPrefix(event) {
  const text = String(event.raw?.summary || event.raw?.body_preview || event.messagePreview || "");
  const match = text.match(/^\s*([A-Za-z0-9_.-]{2,80})\s*:/);
  return match ? match[1] : null;
}

function assignmentMessageBody(event, target = teammateTargetFromAssignment(event)) {
  const text = String(event.raw?.body_preview || event.raw?.summary || event.messagePreview || "").trim();
  if (!text) return "";
  if (target && text.startsWith(`${target}:`)) {
    return text.slice(String(target).length + 1).trim();
  }
  return text;
}

function teammateToolUseSummaries(event) {
  if (event.source !== "sdk_stream") return [];
  const text = String(event.textDelta || event.messagePreview || event.raw?.result || "");
  if (!text) return [];
  const summaries = [];
  const pattern = /(?:^|\n)\s*(?:[├└│]\s*)?([A-Za-z0-9_.-]{2,80})(?:\s+\(([^)\n]{1,120})\))?\s*·\s*(\d+)\s+tool uses?/g;
  let match;
  while ((match = pattern.exec(text))) {
    summaries.push({
      teammateId: match[1],
      role: match[2] || match[1],
      count: Number.parseInt(match[3], 10)
    });
  }
  const seen = new Set();
  return summaries.filter(item => {
    if (!Number.isFinite(item.count) || item.count < 0) return false;
    const key = `${item.teammateId}:${item.count}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function isFailureLike(event) {
  const kind = String(event?.kind || event?.streamEventType || event?.raw?.event_kind || "").toLowerCase();
  const status = String(event?.status || event?.raw?.status || "").toLowerCase();
  return ["failed", "blocked", "rejected"].includes(status) || kind.includes("failed") || kind.includes("rejected") || kind.includes("error") || kind.includes("timeout");
}

function isWaitingLifecycle(event) {
  return isWaitingState(event.raw?.to_status || event.status || event.messagePreview || event.kind);
}

function isWaitingState(value) {
  const text = String(value || "").toLowerCase();
  return text.includes("waiting") || text.includes("idle") || text.includes("message_dispatch_completed") || text.includes("team_waiting");
}

function hasTeammateAttribution(event) {
  const id = event.actor?.teammateId || event.raw?.teammate_id;
  if (!id) return false;
  const role = String(event.actor?.role || event.raw?.agent_type || id || "").toLowerCase();
  return !["bridge-leader", "main-leader", "leader-orchestrator", "runtime", "hook"].includes(role);
}

function actorKey(event) {
  return String(event.actor?.teammateId || event.raw?.teammate_id || event.actor?.displayName || event.raw?.display_name || event.actor?.role || event.sessionId || "runtime");
}

function actorLabel(event) {
  return compact(actorKey(event), 80);
}

function pushQuality(member, quality) {
  const current = SOURCE_QUALITY_RANK[member.sourceQuality] ?? 0;
  const next = SOURCE_QUALITY_RANK[quality] ?? 0;
  if (next >= current) member.sourceQuality = quality;
}

function updateMemberState(member, state) {
  const current = TEAM_STATE_RANK[member.state] ?? 0;
  const next = TEAM_STATE_RANK[state] ?? 0;
  if (next >= current) member.state = state;
}

function pushRaw(member, event) {
  const refs = rawRefs(event);
  member.rawRefs = mergeArrayByKey(member.rawRefs, refs, rawRefKey);
  member.inspector.rawRefs = mergeArrayByKey(member.inspector.rawRefs, refs, rawRefKey);
  if (event && event.eventId) {
    member.inspector.normalizedEvents = [...(member.inspector.normalizedEvents || []), event].slice(-20);
  }
  if (event?.cursor) {
    member.inspector.sourceCursors = mergeArrayByKey(member.inspector.sourceCursors || [], [event.cursor], cursorKey);
  }
}

function importanceForLatest(event) {
  return renderItemFromEvent(event)?.importance || 0;
}

function compareRenderItems(a, b) {
  const at = Date.parse(a.lastTs || "") || 0;
  const bt = Date.parse(b.lastTs || "") || 0;
  if (at !== bt) return bt - at;
  if ((b.importance || 0) !== (a.importance || 0)) return (b.importance || 0) - (a.importance || 0);
  return String(a.displayKey || a.id).localeCompare(String(b.displayKey || b.id));
}

function sortEvents(events) {
  return [...events].sort((a, b) => {
    const at = Date.parse(a.ts || "") || 0;
    const bt = Date.parse(b.ts || "") || 0;
    if (at !== bt) return at - bt;
    const af = a.rawRef?.sourceFile || "";
    const bf = b.rawRef?.sourceFile || "";
    if (af !== bf) return af.localeCompare(bf);
    return Number(a.rawRef?.sourceOffset || a.seq || 0) - Number(b.rawRef?.sourceOffset || b.seq || 0);
  });
}

function rawRefs(event) {
  return event?.rawRef ? [event.rawRef] : [];
}

function evidenceRefs(event) {
  const refs = [];
  if (Array.isArray(event?.evidenceRefs)) refs.push(...event.evidenceRefs);
  const evidence = leaderResultEvidence(event);
  if (evidence && typeof evidence === "object") {
    for (const value of Object.values(evidence)) {
      if (typeof value === "string") refs.push(value);
      else if (Array.isArray(value)) refs.push(...value.filter(item => typeof item === "string"));
    }
  }
  return mergePrimitiveArrays(refs, []);
}

function firstFileRef(event) {
  return Array.isArray(event?.fileRefs) ? event.fileRefs[0] : null;
}

function firstOf(values) {
  return Array.isArray(values) && values.length ? values[0] : null;
}

function hasUsefulFileRefs(event) {
  return Array.isArray(event?.fileRefs) && event.fileRefs.length > 0;
}

function artifactLabel(event) {
  return event.raw?.artifact_id || event.raw?.path || event.raw?.artifact_type || "artifact recorded";
}

function lifecycleTitle(event) {
  const raw = event.raw || {};
  return raw.to_status ? `Lifecycle: ${raw.to_status}` : "Lifecycle transition";
}

function numberOrNull(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function assignDefined(target, source) {
  for (const [key, value] of Object.entries(source || {})) {
    if (value !== undefined && value !== null && value !== "") target[key] = value;
  }
}

function mergeArrayByKey(left = [], right = [], keyFn = value => String(value)) {
  const result = [];
  const seen = new Set();
  for (const item of [...(Array.isArray(left) ? left : []), ...(Array.isArray(right) ? right : [])]) {
    if (!item) continue;
    const key = keyFn(item);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    result.push(item);
  }
  return result;
}

function mergePrimitiveArrays(left = [], right = []) {
  const result = [];
  const seen = new Set();
  for (const value of [...(Array.isArray(left) ? left : []), ...(Array.isArray(right) ? right : [])]) {
    if (value === undefined || value === null || value === "") continue;
    const text = String(value);
    if (seen.has(text)) continue;
    seen.add(text);
    result.push(value);
  }
  return result;
}

function mergeInspectorPayload(...payloads) {
  const merged = {
    rawRefs: [],
    normalizedEvents: [],
    sourceCursors: [],
    evidenceRefs: [],
    snapshotRefs: {}
  };
  for (const payload of payloads) {
    if (!payload || typeof payload !== "object") continue;
    Object.assign(merged, Object.fromEntries(Object.entries(payload).filter(([, value]) => value !== undefined)));
    merged.rawRefs = mergeArrayByKey(merged.rawRefs, payload.rawRefs, rawRefKey);
    merged.normalizedEvents = mergeArrayByKey(merged.normalizedEvents, payload.normalizedEvents, eventInspectorKey).slice(-30);
    merged.sourceCursors = mergeArrayByKey(merged.sourceCursors, payload.sourceCursors, cursorKey);
    merged.evidenceRefs = mergePrimitiveArrays(merged.evidenceRefs, payload.evidenceRefs);
    if (payload.snapshotRefs && typeof payload.snapshotRefs === "object") {
      merged.snapshotRefs = { ...merged.snapshotRefs, ...payload.snapshotRefs };
    }
  }
  return merged;
}

function latestTimestamp(a, b) {
  const at = Date.parse(a || "") || 0;
  const bt = Date.parse(b || "") || 0;
  return bt >= at ? b || a || null : a || b || null;
}

function rawRefKey(ref) {
  if (!ref || typeof ref !== "object") return "";
  return [ref.sourceFile, ref.sourceOffset, ref.sourceSequence, ref.sourceKind].filter(value => value !== undefined && value !== null).join(":");
}

function cursorKey(cursor) {
  if (!cursor || typeof cursor !== "object") return "";
  return [cursor.sourceFile, cursor.sourceOffset, cursor.sourceSequence, cursor.sourceByteOffset].filter(value => value !== undefined && value !== null).join(":");
}

function eventInspectorKey(event) {
  return event?.eventId || rawRefKey(event?.rawRef) || stableHash(event);
}

function displayId(prefix, key) {
  return `${prefix}_${stableHash(key).slice(0, 16)}`;
}

function stableHash(value) {
  const text = typeof value === "string" ? value : stableStringify(value);
  return createHash("sha1").update(String(text)).digest("hex");
}

function stableStringify(value) {
  if (value === null || value === undefined) return "";
  if (typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
}

function compact(value, max = 140) {
  const source = value && typeof value === "object" ? JSON.stringify(value) : value;
  const text = String(source ?? "").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, Math.max(0, max - 3))}...` : text;
}
