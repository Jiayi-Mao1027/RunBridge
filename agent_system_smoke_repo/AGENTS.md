# Smoke Repo AGENTS

This repository exists only to smoke-test the shared agent system.

## Purpose
- provide a safe sandbox for exercising shared `.codex` / `.agents` roles
- avoid modifying real project repositories during worker tests

## Scope
- tiny file edits are allowed only when explicitly requested in the role packet
- all durable outputs should go under `artifacts/runs/<run_id>/`
- do not infer broader experiment semantics from this repo

## Roles
- Refresher may update `specs/`
- Opus may modify only files explicitly listed in the approved packet
- auditors are read-only

## Success
- worker returns valid payload
- runner writes reports, handoffs, receipts, and summaries
- any file edits stay inside the approved smoke scope
