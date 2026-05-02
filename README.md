# AXL Hub

This repository contains two separate services:

- `node_backend/`: a FastAPI app that acts as an AXL-style node for hub coordination.
- `dashboard/`: a React monitoring UI that reads node state from localhost.

The backend now includes workflow discovery, a simple execution orchestrator, per-node x402 payment handling, and crash-safe execution state.

## Backend: AXL-Style Node (FastAPI)

Path: `node_backend/`

### Key Endpoints

- `GET /topology`
	- Returns participants and active summary.
- `CRUD /topology/participants`
	- `GET /topology/participants`
	- `POST /topology/participants`
	- `GET /topology/participants/{participant_id}`
	- `PUT /topology/participants/{participant_id}`
	- `DELETE /topology/participants/{participant_id}`
	- `POST /topology/participants/{participant_id}/touch` (update last seen)
- `CRUD /workstations`
	- `GET /workstations`
	- `POST /workstations`
	- `GET /workstations/{workstation_id}`
	- `PUT /workstations/{workstation_id}`
	- `DELETE /workstations/{workstation_id}`
	- `POST /workstations/{workstation_id}/access` (touch participant access)
- `CRUD /workflows`
	- `POST /workflows`
	- `GET /workflows/{workflow_id}`
	- `GET /workflows/public`
- `GET /clipboard`
- `POST /clipboard`
- `POST /execute`
- `GET /executions/{execution_id}`

### Behavior

- Participant and workstation activity is inferred with TTL.
- Clipboard is a bounded ring buffer.
- Workflows are discoverable with usage count and success rate.
- `POST /execute` accepts either a workflow id or an execution graph and performs deterministic sequential execution with fallback handling.
- Paid workflows use a two-phase reserve/settle flow when `X402_FACILITATOR_URL` is configured.

### Runtime Config

- `ACTIVITY_TTL_SECONDS` (default `60`)
- `CLIPBOARD_MAX_MESSAGES` (default `200`)

### Run Backend

```bash
cd node_backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

## Frontend: Monitoring Dashboard (React)

Path: `dashboard/`

The dashboard polls the node endpoints on localhost and now acts as a small operator console. It displays:

- current participants,
- public workflow discovery with pricing / usage / success metadata,
- a minimal workflow creation form,
- a graph execution form,
- latest execution logs and payment events,
- currently active workstation accesses,
- clipboard messages.

### Demo Flow

1. Create a public workflow with pricing enabled.
2. Load the demo graph in the execution form.
3. Submit the graph to `/execute`.
4. Inspect the latest execution panel for reserve, settle, and release events.

### Frontend Config

- `VITE_NODE_BASE_URL` (default `http://127.0.0.1:8000`)

### Run Frontend

```bash
cd dashboard
npm install
npm run dev
```

Open `http://127.0.0.1:5173`.
