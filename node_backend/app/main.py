from __future__ import annotations

import json
import os
import hashlib
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4
import re
import urllib.request
import urllib.error
import urllib.parse

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Literal


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso8601(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid ISO8601 datetime") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def to_iso8601(value: datetime) -> str:
    return value.isoformat()


ACTIVITY_TTL_SECONDS = int(os.getenv("ACTIVITY_TTL_SECONDS", "60"))
CLIPBOARD_MAX_MESSAGES = int(os.getenv("CLIPBOARD_MAX_MESSAGES", "200"))
STATE_PATH = Path(os.getenv("HUB_STATE_PATH", "data/hub_state.json"))

API_HELP_ENDPOINTS = [
    {
        "method": "GET",
        "path": "/health",
        "description": "Health check for the node service.",
    },
    {
        "method": "GET",
        "path": "/topology",
        "description": "Current topology snapshot including participants.",
    },
    {
        "method": "GET",
        "path": "/topology/participants",
        "description": "List all participants.",
    },
    {
        "method": "POST",
        "path": "/topology/participants",
        "description": "Create a participant.",
    },
    {
        "method": "GET",
        "path": "/workstations",
        "description": "List workstations.",
    },
    {
        "method": "POST",
        "path": "/workstations",
        "description": "Create a workstation.",
    },
    {
        "method": "POST",
        "path": "/workflows",
        "description": "Create a workflow definition.",
    },
    {
        "method": "GET",
        "path": "/workflows/public",
        "description": "List public workflows with discovery stats.",
    },
    {
        "method": "GET",
        "path": "/workflows/{workflow_id}",
        "description": "Fetch a workflow by id.",
    },
    {
        "method": "POST",
        "path": "/execute",
        "description": "Execute a workflow graph.",
    },
    {
        "method": "GET",
        "path": "/executions/{execution_id}",
        "description": "Inspect an execution result.",
    },
    {
        "method": "GET",
        "path": "/clipboard",
        "description": "Read recent clipboard messages.",
    },
]

WORKFLOW_PIPELINE_GUIDE = {
    "goal": "Chain workflow outputs into downstream workflow inputs.",
    "reference_format": "$node.<NODE_ID>.output.<path.to.key>",
    "example": {
        "nodes": [
            {
                "id": "A",
                "workflow_id": "chainlink-price-fetcher",
                "params": {"coin": "btc"},
            },
            {
                "id": "B",
                "workflow_id": "email-send",
                "params": {
                    "email": "siddharthaswarnkar@gmail.com",
                    "content": "$node.A.output.price",
                },
            },
        ],
        "edges": [
            {
                "from_id": "A",
                "to_id": "B",
                "condition": "true",
            }
        ],
    },
    "execution_flow": [
        "Node A runs first and produces output, for example {\"price\": 65432.5}.",
        "Node B resolves $node.A.output.price before execution.",
        "The resolved value is passed into Node B as its content parameter.",
    ],
    "supported_patterns": [
        "Use $node.A.output.price for a single field.",
        "Use $node.A.output.data.nested.key for nested output objects.",
        "Use edge conditions to gate execution, such as true or $node.A.output.price > 1000.",
    ],
}


class ParticipantCreate(BaseModel):
    id: str = Field(min_length=1)
    label: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParticipantUpdate(BaseModel):
    label: str | None = None
    metadata: dict[str, Any] | None = None


class ParticipantTouch(BaseModel):
    seen_at: str | None = None


class WorkstationCreate(BaseModel):
    id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    workflow: dict[str, Any] = Field(default_factory=dict)


class WorkstationUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    metadata: dict[str, Any] | None = None
    workflow: dict[str, Any] | None = None


class WorkstationExecutionCreate(BaseModel):
    actor_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)


class WorkstationAccessCreate(BaseModel):
    participant_id: str = Field(min_length=1)
    seen_at: str | None = None


class ClipboardMessageCreate(BaseModel):
    author_id: str | None = None
    message: str = Field(min_length=1)


class GraphNode(BaseModel):
    id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    on_failure: str | None = None
    retry: dict[str, Any] | None = None


class GraphEdge(BaseModel):
    from_id: str = Field(min_length=1)
    to_id: str = Field(min_length=1)
    condition: str | None = None


class ExecutionGraph(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge]


class ExecuteRequest(BaseModel):
    actor_id: str | None = None
    workflow_id: str | None = None
    execution_graph: ExecutionGraph | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    payment_payloads: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PaymentReservation(BaseModel):
    reservation_id: str
    workflow_id: str
    node_id: str
    amount: str | None = None
    token: str | None = None
    recipient: str | None = None


class ExecutionPaymentState(BaseModel):
    payment_reservations: dict[str, str] = Field(default_factory=dict)
    payment_status: dict[str, str] = Field(default_factory=dict)


class X402Request(BaseModel):
    x402Version: int = 1
    paymentPayload: dict[str, Any]
    paymentRequirements: dict[str, Any]


class X402VerifyResponse(BaseModel):
    is_valid: bool
    payer: dict[str, Any] | None = None
    invalid_reason: dict[str, Any] | str | None = None


class X402SettleResponse(BaseModel):
    success: bool
    error_reason: dict[str, Any] | str | None = None
    payer: dict[str, Any] | None = None
    transaction: dict[str, Any] | str | None = None
    network: str | None = None


class WorkflowCreate(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    execution_type: Literal["keeperhub", "local", "http"] = "local"
    endpoint: str | None = None
    owner_id: str = Field(min_length=1)
    visibility: Literal["public", "private"] = "private"
    pricing: dict[str, Any] = Field(default_factory=dict)


class HubStore:
    def __init__(self, ttl_seconds: int, clipboard_size: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.clipboard_size = clipboard_size
        self.participants: dict[str, dict[str, Any]] = {}
        # atomic workflows store (atomic capabilities)
        self.workflows: dict[str, dict[str, Any]] = {}
        self.workstations: dict[str, dict[str, Any]] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.workstation_access: dict[str, dict[str, datetime]] = {}
        self.clipboard: deque[dict[str, Any]] = deque(maxlen=clipboard_size)
        self.lock = Lock()
        self._load_state()
        with self.lock:
            self._cleanup_stale_executions_locked()

    def _state_payload(self) -> dict[str, Any]:
        return {
            "participants": {
                participant_id: {
                    "id": participant["id"],
                    "label": participant.get("label"),
                    "metadata": participant.get("metadata", {}),
                    "last_seen_at": to_iso8601(participant["last_seen_at"]),
                }
                for participant_id, participant in self.participants.items()
            },
            "workstations": {
                workstation_id: {
                    "id": workstation["id"],
                    "owner_id": workstation.get("owner_id", workstation_id),
                    "name": workstation["name"],
                    "description": workstation.get("description"),
                    "metadata": workstation.get("metadata", {}),
                    "workflow": workstation.get("workflow", {}),
                    "created_at": to_iso8601(workstation["created_at"]),
                    "updated_at": to_iso8601(workstation["updated_at"]),
                }
                for workstation_id, workstation in self.workstations.items()
            },
            "workflows": {
                workflow_id: {
                    "id": workflow.get("id", workflow_id),
                    "name": workflow.get("name"),
                    "description": workflow.get("description"),
                    "metadata": workflow.get("metadata", {}),
                    "input_schema": workflow.get("input_schema", {}),
                    "output_schema": workflow.get("output_schema", {}),
                    "execution_type": workflow.get("execution_type"),
                    "endpoint": workflow.get("endpoint"),
                    "owner_id": workflow.get("owner_id"),
                    "visibility": workflow.get("visibility", "private"),
                    "pricing": workflow.get("pricing", {}),
                    "created_at": to_iso8601(workflow["created_at"]),
                    "updated_at": to_iso8601(workflow["updated_at"]),
                }
                for workflow_id, workflow in self.workflows.items()
            },
            "executions": {
                execution_id: {
                    "id": execution["id"],
                    "workstation_id": execution.get("workstation_id"),
                    "actor_id": execution.get("actor_id"),
                    "input": execution.get("input", {}),
                    "status": execution["status"],
                    "output": execution.get("output"),
                    "root_graph": execution.get("root_graph"),
                    "logs": execution.get("logs", []),
                    "proofs": execution.get("proofs", []),
                    "payment_reservations": execution.get("payment_reservations", {}),
                    "payment_status": execution.get("payment_status", {}),
                    "payment": execution.get("payment", []),
                    "created_at": to_iso8601(execution["created_at"]),
                    "completed_at": to_iso8601(execution["completed_at"]) if execution.get("completed_at") else None,
                }
                for execution_id, execution in self.executions.items()
            },
            "workstation_access": {
                workstation_id: {
                    participant_id: to_iso8601(seen_at)
                    for participant_id, seen_at in access_map.items()
                }
                for workstation_id, access_map in self.workstation_access.items()
            },
            "clipboard": list(self.clipboard),
        }

    def _save_state(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self._state_payload(), indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(STATE_PATH)

    def _load_state(self) -> None:
        if not STATE_PATH.exists():
            return

        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        participants = raw.get("participants", {})
        workstations = raw.get("workstations", {})
        executions = raw.get("executions", {})
        workstation_access = raw.get("workstation_access", {})
        clipboard = raw.get("clipboard", [])

        self.participants = {
            participant_id: {
                "id": item.get("id", participant_id),
                "label": item.get("label"),
                "metadata": item.get("metadata", {}),
                "last_seen_at": parse_iso8601(item["last_seen_at"]),
            }
            for participant_id, item in participants.items()
            if item.get("last_seen_at")
        }

        self.workstations = {
            workstation_id: {
                "id": item.get("id", workstation_id),
                "owner_id": item.get("owner_id", workstation_id),
                "name": item.get("name", workstation_id),
                "description": item.get("description"),
                "metadata": item.get("metadata", {}),
                "workflow": item.get("workflow", {}),
                "created_at": parse_iso8601(item["created_at"]),
                "updated_at": parse_iso8601(item["updated_at"]),
            }
            for workstation_id, item in workstations.items()
            if item.get("created_at") and item.get("updated_at")
        }

        self.workflows = {
            workflow_id: {
                "id": item.get("id", workflow_id),
                "name": item.get("name"),
                "description": item.get("description"),
                "metadata": item.get("metadata", {}),
                "input_schema": item.get("input_schema", {}),
                "output_schema": item.get("output_schema", {}),
                "execution_type": item.get("execution_type"),
                "endpoint": item.get("endpoint"),
                "owner_id": item.get("owner_id"),
                "visibility": item.get("visibility", "private"),
                "pricing": item.get("pricing", {}),
                "created_at": parse_iso8601(item["created_at"]),
                "updated_at": parse_iso8601(item["updated_at"]),
            }
            for workflow_id, item in raw.get("workflows", {}).items()
            if item.get("created_at") and item.get("updated_at")
        }

        self.executions = {
            execution_id: {
                "id": item.get("id", execution_id),
                "workstation_id": item.get("workstation_id"),
                "actor_id": item.get("actor_id"),
                "input": item.get("input", {}),
                "status": item.get("status", "completed"),
                "output": item.get("output"),
                "root_graph": item.get("root_graph"),
                "logs": item.get("logs", []),
                "proofs": item.get("proofs", []),
                "payment_reservations": item.get("payment_reservations", {}),
                "payment_status": item.get("payment_status", {}),
                "payment": item.get("payment", []),
                "created_at": parse_iso8601(item["created_at"]),
                "completed_at": parse_iso8601(item["completed_at"]) if item.get("completed_at") else None,
            }
            for execution_id, item in executions.items()
            if item.get("created_at")
        }

        self.workstation_access = {
            workstation_id: {
                participant_id: parse_iso8601(seen_at)
                for participant_id, seen_at in access_map.items()
            }
            for workstation_id, access_map in workstation_access.items()
        }

        self.clipboard = deque(
            (
                {
                    "id": item.get("id", uuid4().hex),
                    "author_id": item.get("author_id"),
                    "message": item.get("message", ""),
                    "created_at": item.get("created_at", to_iso8601(utc_now())),
                }
                for item in clipboard
                if item.get("message")
            ),
            maxlen=self.clipboard_size,
        )

    def _is_active(self, seen_at: datetime, now: datetime | None = None) -> bool:
        current = now or utc_now()
        age_seconds = (current - seen_at).total_seconds()
        return age_seconds <= self.ttl_seconds

    def _participant_response(self, participant: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": participant["id"],
            "label": participant.get("label"),
            "metadata": participant.get("metadata", {}),
            "last_seen_at": to_iso8601(participant["last_seen_at"]),
            "active": self._is_active(participant["last_seen_at"]),
        }

    def _active_accesses_for(self, workstation_id: str) -> list[dict[str, Any]]:
        now = utc_now()
        access_map = self.workstation_access.get(workstation_id, {})
        active = []
        for participant_id, seen_at in access_map.items():
            if self._is_active(seen_at, now):
                active.append(
                    {
                        "participant_id": participant_id,
                        "seen_at": to_iso8601(seen_at),
                    }
                )
        return active

    def _is_owner(self, workstation: dict[str, Any], actor_id: str | None) -> bool:
        return bool(actor_id) and workstation.get("owner_id") == actor_id

    def _workstation_public_response(self, workstation: dict[str, Any]) -> dict[str, Any]:
        active_accesses = self._active_accesses_for(workstation["id"])
        return {
            "id": workstation["id"],
            "name": workstation["name"],
            "description": workstation.get("description"),
            "metadata": workstation.get("metadata", {}),
            "created_at": to_iso8601(workstation["created_at"]),
            "updated_at": to_iso8601(workstation["updated_at"]),
            "active_accesses": active_accesses,
        }

    def _workstation_owner_response(self, workstation: dict[str, Any]) -> dict[str, Any]:
        public_response = self._workstation_public_response(workstation)
        recent_executions = [
            self._execution_response(execution)
            for execution in sorted(
                (item for item in self.executions.values() if item.get("workstation_id") == workstation["id"]),
                key=lambda item: item["created_at"],
                reverse=True,
            )[:10]
        ]
        public_response.update(
            {
                "owner_id": workstation.get("owner_id", workstation["id"]),
                "workflow": workstation.get("workflow", {}),
                "recent_executions": recent_executions,
            }
        )
        return public_response

    def _workstation_response(self, workstation: dict[str, Any], actor_id: str | None = None) -> dict[str, Any]:
        if self._is_owner(workstation, actor_id):
            return self._workstation_owner_response(workstation)
        return self._workstation_public_response(workstation)

    def _execution_response(self, execution: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": execution["id"],
            "workstation_id": execution.get("workstation_id"),
            "actor_id": execution.get("actor_id"),
            "input": execution.get("input", {}),
            "status": execution["status"],
            "output": execution.get("output"),
            "root_graph": execution.get("root_graph"),
            "logs": execution.get("logs", []),
            "proofs": execution.get("proofs", []),
            "payment_reservations": execution.get("payment_reservations", {}),
            "payment_status": execution.get("payment_status", {}),
            "payment": execution.get("payment", []),
            "created_at": to_iso8601(execution["created_at"]),
            "completed_at": to_iso8601(execution["completed_at"]) if execution.get("completed_at") else None,
        }

    def get_topology(self) -> dict[str, Any]:
        with self.lock:
            participants = [self._participant_response(item) for item in self.participants.values()]
            return {
                "participant_count": len(participants),
                "active_participant_count": sum(1 for item in participants if item["active"]),
                "participants": participants,
                "activity_ttl_seconds": self.ttl_seconds,
            }

    def list_participants(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self._participant_response(item) for item in self.participants.values()]

    def create_participant(self, payload: ParticipantCreate) -> dict[str, Any]:
        with self.lock:
            if payload.id in self.participants:
                raise HTTPException(status_code=409, detail="Participant already exists")
            now = utc_now()
            self.participants[payload.id] = {
                "id": payload.id,
                "label": payload.label,
                "metadata": payload.metadata,
                "last_seen_at": now,
            }
            self._save_state()
            return self._participant_response(self.participants[payload.id])

    def get_participant(self, participant_id: str) -> dict[str, Any]:
        with self.lock:
            participant = self.participants.get(participant_id)
            if participant is None:
                raise HTTPException(status_code=404, detail="Participant not found")
            return self._participant_response(participant)

    def update_participant(self, participant_id: str, payload: ParticipantUpdate) -> dict[str, Any]:
        with self.lock:
            participant = self.participants.get(participant_id)
            if participant is None:
                raise HTTPException(status_code=404, detail="Participant not found")

            if payload.label is not None:
                participant["label"] = payload.label
            if payload.metadata is not None:
                participant["metadata"] = payload.metadata

            self._save_state()
            return self._participant_response(participant)

    def touch_participant(self, participant_id: str, payload: ParticipantTouch) -> dict[str, Any]:
        with self.lock:
            participant = self.participants.get(participant_id)
            if participant is None:
                raise HTTPException(status_code=404, detail="Participant not found")
            participant["last_seen_at"] = parse_iso8601(payload.seen_at) if payload.seen_at else utc_now()
            self._save_state()
            return self._participant_response(participant)

    def delete_participant(self, participant_id: str) -> None:
        with self.lock:
            if participant_id not in self.participants:
                raise HTTPException(status_code=404, detail="Participant not found")

            del self.participants[participant_id]
            for workstation_id in self.workstation_access:
                self.workstation_access[workstation_id].pop(participant_id, None)
            self._save_state()

    def list_workstations(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self._workstation_response(item) for item in self.workstations.values()]

    def create_workstation(self, payload: WorkstationCreate) -> dict[str, Any]:
        with self.lock:
            if payload.id in self.workstations:
                raise HTTPException(status_code=409, detail="Workstation already exists")
            now = utc_now()
            self.workstations[payload.id] = {
                "id": payload.id,
                "owner_id": payload.owner_id,
                "name": payload.name,
                "description": payload.description,
                "metadata": payload.metadata,
                "workflow": payload.workflow,
                "created_at": now,
                "updated_at": now,
            }
            self.workstation_access[payload.id] = {}
            self._save_state()
            return self._workstation_response(self.workstations[payload.id], actor_id=payload.owner_id)

    def create_workflow(self, payload: WorkflowCreate) -> dict[str, Any]:
        with self.lock:
            if payload.id in self.workflows:
                raise HTTPException(status_code=409, detail="Workflow already exists")
            now = utc_now()
            self.workflows[payload.id] = {
                "id": payload.id,
                "name": payload.name,
                "description": payload.description,
                "metadata": payload.metadata,
                "input_schema": payload.input_schema,
                "output_schema": payload.output_schema,
                "execution_type": payload.execution_type,
                "endpoint": payload.endpoint,
                "owner_id": payload.owner_id,
                "visibility": payload.visibility,
                "pricing": payload.pricing,
                "created_at": now,
                "updated_at": now,
            }
            self._save_state()
            return self.workflows[payload.id]

    def get_workflow(self, workflow_id: str, actor_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            wf = self.workflows.get(workflow_id)
            if wf is None:
                raise HTTPException(status_code=404, detail="Workflow not found")
            response = {
                "id": wf["id"],
                "name": wf.get("name"),
                "description": wf.get("description"),
                "metadata": wf.get("metadata", {}),
                "owner_id": wf.get("owner_id"),
                "visibility": wf.get("visibility"),
                "pricing": wf.get("pricing"),
                "usage_count": self._workflow_usage_count(workflow_id),
                "success_rate": self._workflow_success_rate(workflow_id),
            }
            if wf.get("visibility") == "private" and wf.get("owner_id") != actor_id:
                return response
            response.update(
                {
                    "input_schema": wf.get("input_schema", {}),
                    "output_schema": wf.get("output_schema", {}),
                    "execution_type": wf.get("execution_type"),
                    "endpoint": wf.get("endpoint"),
                    "created_at": to_iso8601(wf["created_at"]),
                    "updated_at": to_iso8601(wf["updated_at"]),
                }
            )
            return response

    def _workflow_usage_count(self, workflow_id: str) -> int:
        count = 0
        for execution in self.executions.values():
            root_graph = execution.get("root_graph") or {}
            node_ids = {
                node.get("workflow_id")
                for node in root_graph.get("nodes", [])
                if isinstance(node, dict) and node.get("workflow_id")
            }
            if workflow_id in node_ids:
                count += 1
        return count

    def _workflow_success_count(self, workflow_id: str) -> int:
        count = 0
        for execution in self.executions.values():
            root_graph = execution.get("root_graph") or {}
            node_ids = {
                node.get("workflow_id")
                for node in root_graph.get("nodes", [])
                if isinstance(node, dict) and node.get("workflow_id")
            }
            if workflow_id in node_ids and execution.get("status") == "completed":
                count += 1
        return count

    def _workflow_success_rate(self, workflow_id: str) -> float:
        usage_count = self._workflow_usage_count(workflow_id)
        if usage_count == 0:
            return 0.0
        return round(self._workflow_success_count(workflow_id) / usage_count, 3)

    def list_public_workflows(self) -> list[dict[str, Any]]:
        with self.lock:
            workflows = [
                {
                    "id": wf["id"],
                    "name": wf.get("name"),
                    "description": wf.get("description"),
                    "metadata": wf.get("metadata", {}),
                    "owner_id": wf.get("owner_id"),
                    "visibility": wf.get("visibility"),
                    "pricing": wf.get("pricing"),
                    "usage_count": self._workflow_usage_count(wf["id"]),
                    "success_rate": self._workflow_success_rate(wf["id"]),
                }
                for wf in self.workflows.values()
                if wf.get("visibility") == "public"
            ]
            return sorted(workflows, key=lambda item: (-item["success_rate"], -item["usage_count"], item["id"]))

    def get_workstation(self, workstation_id: str, actor_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            workstation = self.workstations.get(workstation_id)
            if workstation is None:
                raise HTTPException(status_code=404, detail="Workstation not found")
            return self._workstation_response(workstation, actor_id=actor_id)

    def update_workstation(self, workstation_id: str, actor_id: str, payload: WorkstationUpdate) -> dict[str, Any]:
        with self.lock:
            workstation = self.workstations.get(workstation_id)
            if workstation is None:
                raise HTTPException(status_code=404, detail="Workstation not found")
            if not self._is_owner(workstation, actor_id):
                raise HTTPException(status_code=403, detail="Only the owner can modify this workstation")

            if payload.name is not None:
                workstation["name"] = payload.name
            if payload.description is not None:
                workstation["description"] = payload.description
            if payload.metadata is not None:
                workstation["metadata"] = payload.metadata
            if payload.workflow is not None:
                workstation["workflow"] = payload.workflow
            workstation["updated_at"] = utc_now()

            self._save_state()
            return self._workstation_response(workstation, actor_id=actor_id)

    def delete_workstation(self, workstation_id: str, actor_id: str) -> None:
        with self.lock:
            if workstation_id not in self.workstations:
                raise HTTPException(status_code=404, detail="Workstation not found")
            if not self._is_owner(self.workstations[workstation_id], actor_id):
                raise HTTPException(status_code=403, detail="Only the owner can remove this workstation")

            del self.workstations[workstation_id]
            self.workstation_access.pop(workstation_id, None)
            to_remove = [execution_id for execution_id, execution in self.executions.items() if execution.get("workstation_id") == workstation_id]
            for execution_id in to_remove:
                del self.executions[execution_id]
            self._save_state()

    def touch_workstation_access(self, workstation_id: str, payload: WorkstationAccessCreate) -> dict[str, Any]:
        with self.lock:
            if workstation_id not in self.workstations:
                raise HTTPException(status_code=404, detail="Workstation not found")
            if payload.participant_id not in self.participants:
                raise HTTPException(status_code=404, detail="Participant not found")

            seen_at = parse_iso8601(payload.seen_at) if payload.seen_at else utc_now()
            self.workstation_access[workstation_id][payload.participant_id] = seen_at
            self.participants[payload.participant_id]["last_seen_at"] = seen_at
            self.workstations[workstation_id]["updated_at"] = utc_now()

            self._save_state()
            return self._workstation_response(self.workstations[workstation_id])

    def execute_workstation(self, workstation_id: str, payload: WorkstationExecutionCreate) -> dict[str, Any]:
        with self.lock:
            workstation = self.workstations.get(workstation_id)
            if workstation is None:
                raise HTTPException(status_code=404, detail="Workstation not found")

            now = utc_now()
            execution_id = uuid4().hex
            execution = {
                "id": execution_id,
                "workstation_id": workstation_id,
                "actor_id": payload.actor_id,
                "input": payload.input,
                "status": "completed",
                "output": {
                    "message": "Workflow accepted by hub",
                },
                "created_at": now,
                "completed_at": now,
            }
            self.executions[execution_id] = execution
            workstation["updated_at"] = now
            self._save_state()
            return {
                "accepted": True,
                "execution_id": execution_id,
                "workstation_id": workstation_id,
            }

    # --- Orchestrator helpers (MVP, sequential DAG execution) ---
    def _resolve_output_reference(self, ref: str, node_outputs: dict[str, dict[str, Any]]) -> Any:
        # format: $node.<NODE_ID>.output.<path.to.key>
        if not isinstance(ref, str) or not ref.startswith("$node."):
            return ref
        parts = ref.split('.', 3)
        if len(parts) < 4 or parts[0] != "$node":
            return ref
        node_id = parts[1]
        tail = parts[3]
        output = node_outputs.get(node_id, {})
        # traverse tail by dots
        value = output
        for key in tail.split('.'):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value

    def _resolve_params(self, params: dict[str, Any], node_outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for k, v in params.items():
            if isinstance(v, str) and v.startswith("$node."):
                resolved[k] = self._resolve_output_reference(v, node_outputs)
            elif isinstance(v, dict):
                resolved[k] = self._resolve_params(v, node_outputs)
            else:
                resolved[k] = v
        return resolved

    def _safe_eval_condition(self, condition: str, node_outputs: dict[str, dict[str, Any]]) -> bool:
        # Very small evaluator for expressions like "$node.A.output.price > 100"
        if not condition:
            return True
        # replace $node.X.output.Y with their literal values
        pattern = re.compile(r"\$node\.([A-Za-z0-9_-]+)\.output(?:\.([A-Za-z0-9_\.]+))?")

        def _repl(m: re.Match) -> str:
            node = m.group(1)
            rest = m.group(2)
            if rest:
                val = node_outputs.get(node, {})
                for part in rest.split('.'):
                    if isinstance(val, dict) and part in val:
                        val = val[part]
                    else:
                        return 'null'
            else:
                val = node_outputs.get(node, {})
            if isinstance(val, str):
                return f'"{val}"'
            if val is None:
                return 'null'
            return str(val)

        expr = pattern.sub(_repl, condition)
        # allow only simple operators
        if re.search(r"[^0-9\s><=!.\'\"nulltruefalsed\.\-+]", expr):
            return False
        try:
            return bool(eval(expr, {"__builtins__": None}, {}))
        except Exception:
            return False

    def _topo_sort(self, graph: ExecutionGraph) -> list[str]:
        # Kahn's algorithm
        nodes = sorted(n.id for n in graph.nodes)
        incoming: dict[str, int] = {n: 0 for n in nodes}
        adj: dict[str, list[str]] = {n: [] for n in nodes}
        for e in graph.edges:
            if e.to_id in nodes and e.from_id in nodes:
                incoming[e.to_id] = incoming.get(e.to_id, 0) + 1
                adj[e.from_id].append(e.to_id)
        for node_id in adj:
            adj[node_id].sort()
        q = sorted(n for n, deg in incoming.items() if deg == 0)
        order: list[str] = []
        while q:
            n = q.pop(0)
            order.append(n)
            for m in adj.get(n, []):
                incoming[m] -= 1
                if incoming[m] == 0:
                    q.append(m)
            q.sort()
        if len(order) != len(nodes):
            raise HTTPException(status_code=400, detail="Graph has cycles or missing nodes")
        return order

    def _stable_json(self, payload: Any) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    def _execution_fingerprint(self, payload: ExecuteRequest, graph: ExecutionGraph) -> str:
        stable_payload = {
            "actor_id": payload.actor_id,
            "execution_graph": graph.model_dump(mode="json"),
            "idempotency_key": payload.idempotency_key,
            "input": payload.input,
            "workflow_id": payload.workflow_id,
        }
        return hashlib.sha256(self._stable_json(stable_payload).encode("utf-8")).hexdigest()

    def _find_execution_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        for execution in self.executions.values():
            if execution.get("fingerprint") == fingerprint:
                return execution
        return None

    def _facilitator_url(self) -> str | None:
        return os.getenv("X402_FACILITATOR_URL") or os.getenv("FACILITATOR_URL")

    def _x402_network(self, workflow: dict[str, Any]) -> str:
        pricing = self._workflow_pricing(workflow)
        x402 = pricing.get("x402") or {}
        requested_network = str(x402.get("network") or "base-sepolia").strip().lower()
        if requested_network and requested_network != "base-sepolia":
            raise HTTPException(status_code=400, detail="x402 payments are supported only on base-sepolia")
        return "base-sepolia"

    def _build_x402_requirements(self, workflow: dict[str, Any], node_id: str) -> dict[str, Any]:
        pricing = self._workflow_pricing(workflow)
        x402 = pricing.get("x402") or {}
        resource = x402.get("resource") or f"/execute/{node_id}"
        return {
            "scheme": x402.get("scheme", "exact"),
            "network": self._x402_network(workflow),
            "asset": x402.get("asset") or x402.get("token_address") or x402.get("token"),
            "max_amount_required": str(x402.get("price", "0")),
            "resource": resource,
            "description": x402.get("description") or workflow.get("name") or node_id,
            "mime_type": x402.get("mime_type", "application/json"),
            "pay_to": x402.get("recipient") or x402.get("pay_to"),
            "max_timeout_seconds": x402.get("max_timeout_seconds", 60),
            "extra": x402.get("extra", {}),
            "output_schema": workflow.get("output_schema", {}),
        }

    def _post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
            return json.loads(response_body) if response_body else {}

    def _cleanup_stale_executions_locked(self) -> int:
        stale_limit_seconds = int(os.getenv("EXECUTION_STALE_SECONDS", "600"))
        now = utc_now()
        stale_ids = [
            execution_id
            for execution_id, execution in self.executions.items()
            if execution.get("status") == "running"
            and execution.get("created_at")
            and (now - execution["created_at"]).total_seconds() > stale_limit_seconds
        ]

        released_count = 0
        for execution_id in stale_ids:
            execution = self.executions[execution_id]
            for node_id, reservation_id in execution.get("payment_reservations", {}).items():
                if execution.get("payment_status", {}).get(node_id) == "reserved":
                    try:
                        self._release_payment(reservation_id)
                    finally:
                        execution.setdefault("payment_status", {})[node_id] = "released"
                        released_count += 1
            execution["status"] = "aborted"
            execution.setdefault("logs", []).append({"system": "cleanup", "message": "stale execution aborted"})
            execution["finished_at"] = now

        if stale_ids:
            self._save_state()
        return released_count

    def cleanup_stale_executions(self) -> int:
        with self.lock:
            return self._cleanup_stale_executions_locked()

    def _get_coin_price(self, coin: str) -> float | None:
        """Get simulated price for a coin (BTC, ETH, DAI, LINK)."""
        coin_lower = str(coin).lower()
        prices = {
            "btc": 65432.50,
            "eth": 3421.75,
            "dai": 1.00,
            "link": 28.45,
        }
        return prices.get(coin_lower)

    def _send_email(self, email: str, content: str) -> dict[str, Any]:
        """Simulate sending an email. Returns success."""
        # In production, this would call a real email service (SendGrid, AWS SES, etc.)
        # For now, we just log it and return success
        print(f"[EMAIL] To: {email} | Content: {content[:100]}")
        return {
            "success": True,
            "message_id": f"msg_{uuid4().hex[:8]}",
            "recipient": email,
        }

    def _call_adapter(self, workflow: dict[str, Any], input_payload: dict[str, Any]) -> dict[str, Any]:
        etype = workflow.get("execution_type")
        endpoint = workflow.get("endpoint")
        workflow_id = workflow.get("id")
        
        # local adapter: echo input as output
        if etype == "local" or etype is None:
            return {"status": "completed", "output": input_payload}
        
        if etype == "http":
            if not endpoint:
                return {"status": "failed", "error": "no endpoint configured"}
            try:
                data = json.dumps(input_payload).encode("utf-8")
                req = urllib.request.Request(endpoint, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read().decode("utf-8")
                    try:
                        parsed = json.loads(body)
                    except Exception:
                        parsed = {"raw": body}
                    return {"status": "completed", "output": parsed}
            except urllib.error.URLError as exc:
                return {"status": "failed", "error": str(exc)}
        
        if etype == "keeperhub":
            # KeeperHub adapter: simulate known workflows with realistic data
            proof = uuid4().hex
            output = input_payload
            
            # Chainlink price fetcher: return price for the requested coin
            if workflow_id == "chainlink-price-fetcher":
                coin = input_payload.get("coin")
                if coin:
                    price = self._get_coin_price(coin)
                    if price is not None:
                        output = {"price": price}
                    else:
                        return {"status": "failed", "error": f"unsupported coin: {coin}"}
                else:
                    return {"status": "failed", "error": "coin parameter required"}
            
            # Email send: simulate sending an email
            elif workflow_id == "email-send":
                email = input_payload.get("email")
                content = input_payload.get("content")
                if not email or not content:
                    return {"status": "failed", "error": "email and content parameters required"}
                result = self._send_email(str(email), str(content))
                output = result
            
            return {"status": "completed", "output": output, "proof": proof}
        
        return {"status": "failed", "error": "unsupported execution_type"}

    def _workflow_pricing(self, workflow: dict[str, Any]) -> dict[str, Any]:
        pricing = workflow.get("pricing") or {}
        return pricing if isinstance(pricing, dict) else {}

    def _reserve_payment(
        self,
        workflow: dict[str, Any],
        node_id: str,
        actor_id: str | None,
        payment_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pricing = self._workflow_pricing(workflow)
        if not pricing.get("enabled"):
            return {"approved": True, "reservation_id": None}

        x402 = pricing.get("x402") or {}
        if not isinstance(x402, dict):
            return {"approved": False, "error": "invalid pricing configuration"}

        facilitator_url = self._facilitator_url()
        requirements = self._build_x402_requirements(workflow, node_id)

        # Real x402 path: require a facilitator, a payment payload, and a token asset.
        if facilitator_url:
            if not payment_payload:
                return {"approved": False, "error": "missing x402 payment payload"}
            if not requirements.get("asset"):
                return {"approved": False, "error": "missing x402 asset/token address"}
            try:
                verify_response = self._post_json(
                    f"{facilitator_url.rstrip('/')}/verify",
                    {
                        "x402Version": 1,
                        "paymentPayload": payment_payload,
                        "paymentRequirements": requirements,
                    },
                )
            except Exception as exc:
                return {"approved": False, "error": f"x402 verify failed: {exc}"}

            if not verify_response.get("is_valid"):
                return {
                    "approved": False,
                    "error": verify_response.get("invalid_reason") or "payment verification failed",
                }

            reservation_id = hashlib.sha256(
                self._stable_json(
                    {
                        "node_id": node_id,
                        "workflow_id": workflow.get("id"),
                        "payment_payload": payment_payload,
                        "requirements": requirements,
                    }
                ).encode("utf-8")
            ).hexdigest()
            return {
                "approved": True,
                "reservation_id": reservation_id,
                "amount": requirements.get("max_amount_required"),
                "token": requirements.get("asset"),
                "recipient": requirements.get("pay_to"),
                "payment_payload": payment_payload,
                "requirements": requirements,
                "x402_mode": "facilitator",
            }

        # Fallback path: keep the current demo behavior when no facilitator payload is present.
        reservation_id = uuid4().hex
        return {
            "approved": True,
            "reservation_id": reservation_id,
            "amount": x402.get("price"),
            "token": x402.get("token"),
            "recipient": x402.get("recipient"),
            "x402_mode": "stub",
        }

    def _settle_payment(self, reservation_id: str, payment_payload: dict[str, Any] | None = None, requirements: dict[str, Any] | None = None) -> dict[str, Any]:
        facilitator_url = self._facilitator_url()
        if facilitator_url and payment_payload and requirements:
            try:
                settle_response = self._post_json(
                    f"{facilitator_url.rstrip('/')}/settle",
                    {
                        "x402Version": 1,
                        "paymentPayload": payment_payload,
                        "paymentRequirements": requirements,
                    },
                )
            except Exception as exc:
                return {"success": False, "error": f"x402 settle failed: {exc}", "reservation_id": reservation_id}

            if not settle_response.get("success"):
                return {
                    "success": False,
                    "error": settle_response.get("error_reason") or "payment settlement failed",
                    "reservation_id": reservation_id,
                }
            return {
                "success": True,
                "settlement_id": settle_response.get("transaction") or uuid4().hex,
                "reservation_id": reservation_id,
                "network": settle_response.get("network"),
                "payer": settle_response.get("payer"),
                "x402_mode": "facilitator",
            }

        # Demo fallback: settlement is accepted locally.
        return {"success": True, "settlement_id": uuid4().hex, "reservation_id": reservation_id, "x402_mode": "stub"}

    def _release_payment(self, reservation_id: str) -> dict[str, Any]:
        # placeholder for facilitator release/void call
        return {"success": True, "release_id": uuid4().hex, "reservation_id": reservation_id}

    def _resolve_workflow_for_node(self, node: GraphNode) -> dict[str, Any] | None:
        if node.workflow_id in self.workflows:
            return self.workflows[node.workflow_id]
        workstation = self.workstations.get(node.workflow_id)
        if workstation and isinstance(workstation.get("workflow"), dict):
            return workstation.get("workflow")
        return None

    def _graph_paid_nodes(self, graph: ExecutionGraph) -> list[str]:
        paid_nodes: list[str] = []
        for node in graph.nodes:
            workflow = self._resolve_workflow_for_node(node)
            if workflow and self._workflow_pricing(workflow).get("enabled"):
                paid_nodes.append(node.id)
        return paid_nodes

    def _run_execution_node(
        self,
        *,
        node_id: str,
        merge_root_input: bool,
        node_map: dict[str, GraphNode],
        payload: ExecuteRequest,
        node_outputs: dict[str, dict[str, Any]],
        payment_reservations: dict[str, str],
        payment_status: dict[str, str],
        payment_details: dict[str, dict[str, Any]],
        record: dict[str, Any],
        executed_nodes: set[str],
    ) -> bool:
        if node_id in executed_nodes:
            return True

        node = node_map.get(node_id)
        if node is None:
            record["status"] = "failed"
            record["logs"].append({"node": node_id, "error": "node not found"})
            return False

        wf_def = self._resolve_workflow_for_node(node)
        if wf_def is None:
            record["status"] = "failed"
            record["logs"].append({"node": node_id, "error": "workflow not found"})
            return False

        resolved_params = self._resolve_params(node.params or {}, node_outputs)
        resolved_input = {**payload.input, **resolved_params} if merge_root_input else resolved_params

        result = self._call_adapter(wf_def, resolved_input)
        if result.get("status") != "completed":
            record["logs"].append({"node": node_id, "result": result})
            reservation_id = payment_reservations.get(node_id)
            if reservation_id and payment_status.get(node_id) == "reserved":
                self._release_payment(reservation_id)
                payment_status[node_id] = "released"
                record["payment"].append(
                    {
                        "node": node_id,
                        "action": "release",
                        "reservation_id": reservation_id,
                    }
                )

            fallback_id = node.on_failure
            fallback_node = node_map.get(fallback_id) if fallback_id else None
            if fallback_node is not None and fallback_id not in executed_nodes:
                record["logs"].append({"node": node_id, "fallback": fallback_id})
                executed_nodes.add(node_id)
                return self._run_execution_node(
                    node_id=fallback_id,
                    merge_root_input=False,
                    node_map=node_map,
                    payload=payload,
                    node_outputs=node_outputs,
                    payment_reservations=payment_reservations,
                    payment_status=payment_status,
                    payment_details=payment_details,
                    record=record,
                    executed_nodes=executed_nodes,
                )

            record["status"] = "failed"
            return False

        node_outputs[node_id] = result.get("output", {})
        record["logs"].append({"node": node_id, "output": node_outputs[node_id]})
        if result.get("proof"):
            record["proofs"].append({"node": node_id, "proof": result.get("proof")})

        reservation_id = payment_reservations.get(node_id)
        if reservation_id and payment_status.get(node_id) == "reserved":
            node_payment = payment_details.get(node_id, {})
            settlement = self._settle_payment(
                reservation_id,
                node_payment.get("payment_payload"),
                node_payment.get("requirements"),
            )
            if settlement.get("success"):
                payment_status[node_id] = "settled"
                record["payment"].append(
                    {
                        "node": node_id,
                        "action": "settle",
                        "reservation_id": reservation_id,
                        "settlement_id": settlement.get("settlement_id"),
                    }
                )
            else:
                self._release_payment(reservation_id)
                payment_status[node_id] = "released"
                record["payment"].append(
                    {
                        "node": node_id,
                        "action": "release",
                        "reservation_id": reservation_id,
                        "error": settlement.get("error"),
                    }
                )
                record["logs"].append({"node": node_id, "error": settlement.get("error")})
                record["status"] = "failed"
                return False

        executed_nodes.add(node_id)
        return True

    def execute(self, payload: ExecuteRequest) -> dict[str, Any]:
        with self.lock:
            self._cleanup_stale_executions_locked()

            # build an execution graph from workflow_id or provided graph
            if payload.execution_graph is None and payload.workflow_id is None:
                raise HTTPException(status_code=400, detail="workflow_id or execution_graph required")

            # if workflow_id provided, build a single-node graph referencing that workflow
            if payload.execution_graph is None and payload.workflow_id:
                if payload.workflow_id in self.workflows:
                    node = GraphNode(id="root", workflow_id=payload.workflow_id, params={})
                elif payload.workflow_id in self.workstations:
                    # workstation contains workflow blob; treat as workflow
                    node = GraphNode(id="root", workflow_id=payload.workflow_id, params={})
                else:
                    raise HTTPException(status_code=404, detail="workflow not found")
                graph = ExecutionGraph(nodes=[node], edges=[])
            else:
                graph = payload.execution_graph

            fingerprint = self._execution_fingerprint(payload, graph)
            existing_execution = self._find_execution_by_fingerprint(fingerprint)
            if existing_execution is not None:
                return {
                    "execution_id": existing_execution["id"],
                    "status": existing_execution.get("status"),
                    "outputs": existing_execution.get("outputs", {}),
                    "fingerprint": existing_execution.get("fingerprint"),
                    "idempotent": True,
                }

            # index nodes
            node_map = {n.id: n for n in graph.nodes}
            order = self._topo_sort(graph)
            node_outputs: dict[str, dict[str, Any]] = {}
            payment_reservations: dict[str, str] = {}
            payment_status: dict[str, str] = {}
            payment_details: dict[str, dict[str, Any]] = {}
            executed_nodes: set[str] = set()
            execution_id = uuid4().hex
            now = utc_now()
            record: dict[str, Any] = {
                "id": execution_id,
                "root_graph": graph.dict(),
                "inputs": payload.input,
                "outputs": {},
                "status": "running",
                "logs": [],
                "proofs": [],
                "payment": [],
                "payment_reservations": payment_reservations,
                "payment_status": payment_status,
                "fingerprint": fingerprint,
                "idempotency_key": payload.idempotency_key,
                "created_at": now,
                "finished_at": None,
            }
            self.executions[execution_id] = record
            self._save_state()

            # Phase 1: reserve payments for all paid nodes before any execution starts.
            reserved_nodes: list[str] = []
            for node_id in order:
                node = node_map[node_id]
                workflow = self._resolve_workflow_for_node(node)
                if workflow is None:
                    record["status"] = "failed"
                    record["logs"].append({"node": node_id, "error": "workflow not found"})
                    break
                pricing = self._workflow_pricing(workflow)
                if not pricing.get("enabled"):
                    continue

                node_payment_payload = payload.payment_payloads.get(node_id)
                reservation = self._reserve_payment(workflow, node_id, payload.actor_id, node_payment_payload)
                if not reservation.get("approved"):
                    for reserved_node_id in reserved_nodes:
                        reservation_id = payment_reservations.get(reserved_node_id)
                        if reservation_id:
                            self._release_payment(reservation_id)
                            payment_status[reserved_node_id] = "released"
                    record["status"] = "failed"
                    record["logs"].append({"node": node_id, "error": "payment_preflight_failed"})
                    record["finished_at"] = utc_now()
                    self.executions[execution_id] = record
                    self._save_state()
                    raise HTTPException(status_code=402, detail="Payment preflight failed")

                reservation_id = reservation.get("reservation_id")
                if reservation_id:
                    payment_reservations[node_id] = reservation_id
                    payment_status[node_id] = "reserved"
                    payment_details[node_id] = reservation
                    reserved_nodes.append(node_id)
                    record["payment"].append(
                        {
                            "node": node_id,
                            "action": "reserve",
                            "reservation_id": reservation_id,
                            "amount": reservation.get("amount"),
                            "token": reservation.get("token"),
                            "recipient": reservation.get("recipient"),
                        }
                    )

            if record["status"] == "failed":
                for reserved_node_id in reserved_nodes:
                    reservation_id = payment_reservations.get(reserved_node_id)
                    if reservation_id and payment_status.get(reserved_node_id) == "reserved":
                        self._release_payment(reservation_id)
                        payment_status[reserved_node_id] = "released"
                record["finished_at"] = utc_now()
                self.executions[execution_id] = record
                self._save_state()
                return {"execution_id": execution_id, "status": record["status"], "outputs": record.get("outputs", {})}

            for index, node_id in enumerate(order):
                if node_id in executed_nodes:
                    continue

                success = self._run_execution_node(
                    node_id=node_id,
                    merge_root_input=index == 0,
                    node_map=node_map,
                    payload=payload,
                    node_outputs=node_outputs,
                    payment_reservations=payment_reservations,
                    payment_status=payment_status,
                    payment_details=payment_details,
                    record=record,
                    executed_nodes=executed_nodes,
                )
                if not success:
                    break

            if record["status"] != "failed":
                record["status"] = "completed"
                record["outputs"] = node_outputs
            record["finished_at"] = utc_now()
            self.executions[execution_id] = record
            self._save_state()
            return {"execution_id": execution_id, "status": record["status"], "outputs": record.get("outputs", {})}

    def list_workstation_executions(self, workstation_id: str, actor_id: str) -> list[dict[str, Any]]:
        with self.lock:
            workstation = self.workstations.get(workstation_id)
            if workstation is None:
                raise HTTPException(status_code=404, detail="Workstation not found")
            if not self._is_owner(workstation, actor_id):
                raise HTTPException(status_code=403, detail="Only the owner can inspect executions")
            executions = [
                self._execution_response(item)
                for item in sorted(
                    (execution for execution in self.executions.values() if execution.get("workstation_id") == workstation_id),
                    key=lambda item: item["created_at"],
                    reverse=True,
                )
            ]
            return executions

    def get_workstation_execution(self, workstation_id: str, execution_id: str, actor_id: str) -> dict[str, Any]:
        with self.lock:
            workstation = self.workstations.get(workstation_id)
            if workstation is None:
                raise HTTPException(status_code=404, detail="Workstation not found")
            if not self._is_owner(workstation, actor_id):
                raise HTTPException(status_code=403, detail="Only the owner can inspect executions")
            execution = self.executions.get(execution_id)
            if execution is None or execution.get("workstation_id") != workstation_id:
                raise HTTPException(status_code=404, detail="Execution not found")
            return self._execution_response(execution)

    def list_clipboard(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self.lock:
            items = list(self.clipboard)
            if limit is not None:
                items = items[-limit:]
            return items

    def post_clipboard(self, payload: ClipboardMessageCreate) -> dict[str, Any]:
        with self.lock:
            if payload.author_id and payload.author_id in self.participants:
                self.participants[payload.author_id]["last_seen_at"] = utc_now()

            entry = {
                "id": uuid4().hex,
                "author_id": payload.author_id,
                "message": payload.message,
                "created_at": to_iso8601(utc_now()),
            }
            self.clipboard.append(entry)
            self._save_state()
            return entry


store = HubStore(ttl_seconds=ACTIVITY_TTL_SECONDS, clipboard_size=CLIPBOARD_MAX_MESSAGES)

app = FastAPI(title="AXL Hub Node", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# AXL registry integration: register services on startup and unregister on shutdown
AXL_REGISTRY_URL = os.getenv("AXL_REGISTRY_URL")
AXL_MCP_URL = os.getenv("AXL_MCP_URL")
_registered_services: list[str] = []

SERVICE_REGISTRY_MAP: dict[str, str] = {
    "topology": "/topology",
    "workstations": "/workstations",
    "workflows": "/workflows",
    "workflows_public": "/workflows/public",
    "execute": "/execute",
    "executions": "/executions",
    "clipboard": "/clipboard",
}

def _axl_register(service_name: str, endpoint: str, registry_url: str) -> bool:
    try:
        payload = json.dumps({"service": service_name, "endpoint": endpoint}).encode("utf-8")
        req = urllib.request.Request(f"{registry_url.rstrip('/')}/register", data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True
    except Exception as exc:
        print(f"AXL register failed for {service_name}: {exc}")
        return False


def _axl_unregister(service_name: str, registry_url: str) -> bool:
    try:
        url = f"{registry_url.rstrip('/')}/register/{urllib.parse.quote(service_name, safe='')}"
        req = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True
    except Exception as exc:
        print(f"AXL unregister failed for {service_name}: {exc}")
        return False


@app.on_event("startup")
def _axl_register_services_on_startup() -> None:
    if not AXL_REGISTRY_URL or not AXL_MCP_URL:
        return
    for name in SERVICE_REGISTRY_MAP.keys():
        success = _axl_register(name, AXL_MCP_URL, AXL_REGISTRY_URL)
        if success:
            _registered_services.append(name)


@app.on_event("shutdown")
def _axl_unregister_services_on_shutdown() -> None:
    if not AXL_REGISTRY_URL:
        return
    for name in list(_registered_services):
        _axl_unregister(name, AXL_REGISTRY_URL)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/help")
def help_endpoints() -> dict[str, Any]:
    return {
        "service": "AXL Hub Node",
        "version": app.version,
        "endpoints": API_HELP_ENDPOINTS,
    }


@app.get("/help-workflow")
def help_workflow() -> dict[str, Any]:
    return WORKFLOW_PIPELINE_GUIDE


@app.get("/topology")
def get_topology() -> dict[str, Any]:
    return store.get_topology()


@app.get("/topology/participants")
def list_participants() -> dict[str, list[dict[str, Any]]]:
    return {"participants": store.list_participants()}


@app.post("/topology/participants", status_code=201)
def create_participant(payload: ParticipantCreate) -> dict[str, Any]:
    return store.create_participant(payload)


@app.get("/topology/participants/{participant_id}")
def get_participant(participant_id: str) -> dict[str, Any]:
    return store.get_participant(participant_id)


@app.put("/topology/participants/{participant_id}")
def update_participant(participant_id: str, payload: ParticipantUpdate) -> dict[str, Any]:
    return store.update_participant(participant_id, payload)


@app.post("/topology/participants/{participant_id}/touch")
def touch_participant(participant_id: str, payload: ParticipantTouch) -> dict[str, Any]:
    return store.touch_participant(participant_id, payload)


@app.delete("/topology/participants/{participant_id}", status_code=204, response_class=Response)
def delete_participant(participant_id: str) -> Response:
    store.delete_participant(participant_id)
    return Response(status_code=204)


@app.get("/workstations")
def list_workstations() -> dict[str, list[dict[str, Any]]]:
    return {"workstations": store.list_workstations()}


@app.post("/workstations", status_code=201)
def create_workstation(payload: WorkstationCreate) -> dict[str, Any]:
    return store.create_workstation(payload)


@app.post("/workflows", status_code=201)
def create_workflow(payload: WorkflowCreate) -> dict[str, Any]:
    return store.create_workflow(payload)


@app.get("/workflows/public")
def list_public_workflows() -> dict[str, Any]:
    return {"workflows": store.list_public_workflows()}


@app.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str, actor_id: str | None = Query(default=None)) -> dict[str, Any]:
    return store.get_workflow(workflow_id, actor_id=actor_id)


@app.get("/workstations/{workstation_id}")
def get_workstation(workstation_id: str, actor_id: str | None = Query(default=None)) -> dict[str, Any]:
    return store.get_workstation(workstation_id, actor_id=actor_id)


@app.put("/workstations/{workstation_id}")
def update_workstation(workstation_id: str, actor_id: str = Query(...), payload: WorkstationUpdate = ... ) -> dict[str, Any]:
    return store.update_workstation(workstation_id, actor_id, payload)


@app.delete("/workstations/{workstation_id}", status_code=204, response_class=Response)
def delete_workstation(workstation_id: str, actor_id: str = Query(...)) -> Response:
    store.delete_workstation(workstation_id, actor_id)
    return Response(status_code=204)


@app.post("/workstations/{workstation_id}/access")
def touch_workstation_access(workstation_id: str, payload: WorkstationAccessCreate) -> dict[str, Any]:
    return store.touch_workstation_access(workstation_id, payload)


@app.post("/workstations/{workstation_id}/execute")
def execute_workstation(workstation_id: str, payload: WorkstationExecutionCreate) -> dict[str, Any]:
    return store.execute_workstation(workstation_id, payload)


@app.post("/execute")
def execute(payload: ExecuteRequest) -> dict[str, Any]:
    return store.execute(payload)


@app.get("/executions/{execution_id}")
def get_execution(execution_id: str) -> dict[str, Any]:
    execution = store.executions.get(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return store._execution_response(execution)


@app.get("/workstations/{workstation_id}/executions")
def list_workstation_executions(workstation_id: str, actor_id: str = Query(...)) -> dict[str, Any]:
    return {"executions": store.list_workstation_executions(workstation_id, actor_id)}


@app.get("/workstations/{workstation_id}/executions/{execution_id}")
def get_workstation_execution(workstation_id: str, execution_id: str, actor_id: str = Query(...)) -> dict[str, Any]:
    return store.get_workstation_execution(workstation_id, execution_id, actor_id)


@app.get("/clipboard")
def list_clipboard(limit: int | None = Query(default=None, ge=1)) -> dict[str, Any]:
    messages = store.list_clipboard(limit=limit)
    return {
        "max_messages": CLIPBOARD_MAX_MESSAGES,
        "count": len(messages),
        "messages": messages,
    }


@app.post("/clipboard", status_code=201)
def post_clipboard(payload: ClipboardMessageCreate) -> dict[str, Any]:
    return store.post_clipboard(payload)
