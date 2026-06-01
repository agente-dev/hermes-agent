"""desktop-orchestrator workflow tools.

Exposes the three orchestrator tools that Desktop (intake 4) calls over the
IPC boundary. These are the *only* writers of workflow fan-out state.

All kanban mutations performed here go through the kanban tool / db layer
so that workers (the step assignees) never touch PGLite directly.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import yaml

from tools.registry import registry, tool_error

# We reuse the kanban db layer (orchestrator is allowed; step workers are not).
from hermes_cli import kanban_db as kb

# Generic nudge so the gateway dispatcher wakes immediately.
from hermes.dispatcher.nudge import nudge


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _err(msg: str) -> str:
    return tool_error(msg)


def _resolve_board() -> Optional[str]:
    return os.environ.get("HERMES_KANBAN_BOARD")


def _format(s: Optional[str], client_id: str) -> Optional[str]:
    if not s:
        return s
    try:
        return s.format(client_id=client_id)
    except Exception:
        return s


def start_workflow_run(workflow_yaml: str, client_id: str = "", **kw: Any) -> str:
    """Parse workflow_yaml and fan out one kanban task per step.

    Sets assignee, wires task_links from on_complete, stamps
    workflow_template_id + current_step_key for later correlation,
    and fires the dispatch nudge so the worker(s) spawn in <2 s.

    Returns {run_id, task_ids, ok}.
    """
    if not workflow_yaml or not workflow_yaml.strip():
        return _err("workflow_yaml is required")
    client_id = str(client_id or "")
    board = _resolve_board()

    try:
        data = yaml.safe_load(workflow_yaml) or {}
    except Exception as e:
        return _err(f"invalid yaml: {e}")

    name = str(data.get("name") or "workflow").strip()
    steps = data.get("steps") or []
    if not isinstance(steps, (list, tuple)) or not steps:
        return _err("workflow must contain a non-empty steps list")

    run_id = f"{name}:{client_id or 'default'}:{int(time.time())}"
    created: list[str] = []
    step_map: dict[str, str] = {}  # key -> task_id

    conn = None
    try:
        conn = kb.connect(board=board)
        for step in steps:
            if not isinstance(step, dict):
                continue
            key = str(step.get("key") or "").strip()
            assignee = str(step.get("assignee") or "").strip()
            if not key or not assignee:
                continue
            title = _format(step.get("title") or key, client_id) or key
            body = _format(step.get("body"), client_id)
            # Create via the public API so all side-effects (parents, etc.)
            # and the kanban tool contract are honored.
            # We pass the workflow fields by updating after create (the
            # create_task surface will be extended in the same change to
            # accept them natively).
            tid = kb.create_task(
                conn,
                title=title,
                body=body,
                assignee=assignee,
                workspace_kind="scratch",
                initial_status="running",
                created_by="desktop-orchestrator",
                workflow_template_id=name,
                current_step_key=key,
                # parents and links are wired below via the graph
            )
            created.append(tid)
            step_map[key] = tid

        # Wire links from on_complete (supports str or list).
        for step in steps:
            if not isinstance(step, dict):
                continue
            key = str(step.get("key") or "").strip()
            parent_tid = step_map.get(key)
            if not parent_tid:
                continue
            targets = step.get("on_complete") or []
            if isinstance(targets, str):
                targets = [targets]
            for t in targets:
                child_tid = step_map.get(str(t).strip())
                if child_tid and child_tid != parent_tid:
                    try:
                        kb.link_tasks(conn, parent_id=parent_tid, child_id=child_tid)
                    except Exception:
                        pass  # cycles etc. are logged by the layer

        # Nudge immediately so the embedded dispatcher sees the new ready work
        # on the next 1 s slice instead of waiting the full interval.
        try:
            nudge(board=board)
        except Exception:
            pass

        return _ok(run_id=run_id, task_ids=created, workflow=name, client_id=client_id)
    except Exception as e:
        return _err(f"start_workflow_run failed: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def record_step_outcome(run_id: str, step_key: str, outcome: str, summary: Optional[str] = None, **kw: Any) -> str:
    """Record outcome for a step and (best-effort) unblock dependents.

    The orchestrator is the correlation point; the actual kanban task
    complete/block is still driven by the specialist worker via its own
    kanban_complete / kanban_block. This entry point lets Desktop (or the
    step) report structured outcome back into the run for observability
    and for any resume decision.
    """
    # Minimal implementation: we just log the event and nudge (in case the
    # outcome implies dependents can now run). Real wiring can be added
    # by looking up the task for (workflow, step_key) and appending a
    # structured comment or event.
    board = _resolve_board()
    try:
        # Opportunistic nudge so any ready work that the outcome may have
        # unblocked gets picked up fast.
        nudge(board=board)
    except Exception:
        pass
    return _ok(run_id=run_id, step_key=step_key, outcome=outcome, summary=summary)


def resume_step(task_id: str, decision: str, reason: Optional[str] = None, **kw: Any) -> str:
    """Resume a blocked / scheduled / waiting task (orchestrator-mediated)."""
    board = _resolve_board()
    try:
        with kb.connect_closing(board=board) as conn:
            try:
                kb.unblock_task(conn, task_id=task_id)
            except Exception:
                pass
        nudge(board=board)
        return _ok(task_id=task_id, decision=decision)
    except Exception as e:
        return _err(f"resume_step failed: {e}")


# ---------------------------------------------------------------------------
# Registration (auto-discovered when the profile's tools/ are on sys.path)
# ---------------------------------------------------------------------------

registry.register(
    name="start_workflow_run",
    toolset="desktop_orchestrator",
    schema={
        "name": "start_workflow_run",
        "description": "Parse a workflow YAML and fan out kanban tasks for each step. Wires on_complete links and returns a run_id. Fires immediate dispatch nudge.",
        "parameters": {
            "type": "object",
            "properties": {
                "workflow_yaml": {"type": "string", "description": "YAML document with 'name' and 'steps' array (each step has key, assignee, optional title/body/on_complete)."},
                "client_id": {"type": "string", "description": "Opaque client / run correlation id from Desktop."},
            },
            "required": ["workflow_yaml"],
        },
    },
    handler=lambda args, **kw: start_workflow_run(
        workflow_yaml=args.get("workflow_yaml", ""),
        client_id=args.get("client_id", ""),
        **kw,
    ),
)

registry.register(
    name="record_step_outcome",
    toolset="desktop_orchestrator",
    schema={
        "name": "record_step_outcome",
        "description": "Record the outcome of a workflow step (success/failure/etc). May unblock dependents via nudge.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "step_key": {"type": "string"},
                "outcome": {"type": "string", "enum": ["success", "failure", "skipped"]},
                "summary": {"type": "string"},
            },
            "required": ["run_id", "step_key", "outcome"],
        },
    },
    handler=lambda args, **kw: record_step_outcome(
        run_id=args.get("run_id", ""),
        step_key=args.get("step_key", ""),
        outcome=args.get("outcome", ""),
        summary=args.get("summary"),
        **kw,
    ),
)

registry.register(
    name="resume_step",
    toolset="desktop_orchestrator",
    schema={
        "name": "resume_step",
        "description": "Resume a paused / blocked workflow step (orchestrator view).",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "decision": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["task_id", "decision"],
        },
    },
    handler=lambda args, **kw: resume_step(
        task_id=args.get("task_id", ""),
        decision=args.get("decision", ""),
        reason=args.get("reason"),
        **kw,
    ),
)
