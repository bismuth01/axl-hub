# AXL Hub

AXL Hub is a decentralized execution node plus operator dashboard where agents can:

- publish reusable workflows,
- compose workflow graphs that call other workflows,
- route outputs between workflow nodes,
- monetize execution via x402 pricing,
- expose capabilities through MCP/A2A-style integration points.

Humans use the dashboard to monitor topology, discover public workflows, create new workflows, and run graph executions.

## High-Level Architecture

This repo has two services:

- `node_backend/`: FastAPI execution node and workflow registry.
- `dashboard/`: React + Vite operator UI.

Core model:

- Workflows are atomic capabilities (`local`, `http`, or `keeperhub`).
- Execution graphs are DAGs of workflow nodes.
- Node outputs can be piped into downstream node params.
- Paid workflows reserve/settle/release payment events during execution.

## Network Integration: MCP + A2A + Public Peer Dialing

AXL Hub is designed to connect into a broader AXL network via MCP and A2A integration patterns:

- MCP endpoint advertisement is configured with `AXL_MCP_URL`.
- Registry registration is configured with `AXL_REGISTRY_URL`.
- On backend startup, services are registered with the registry.
- On backend shutdown, services are unregistered.
- The frontend can display a public peer id via `VITE_PUBLIC_PEER_ID`.

With a public peer id visible in the UI, remote agents can dial/discover the node identity more easily, which opens up many composition and routing possibilities across the network.

## Backend Guide (FastAPI)

Service path: `node_backend/`

### Key Endpoints

- `GET /health`
- `GET /help`
- `GET /help-workflow`
- `GET /topology`
- `CRUD /topology/participants`
- `CRUD /workstations` (kept in backend for compatibility)
- `CRUD /workflows`
- `GET /workflows/public`
- `POST /execute`
- `GET /executions/{execution_id}`
- `GET/POST /clipboard`

### Execution Behavior

- Graphs execute in deterministic topological order.
- `on_failure` supports fallback node routing.
- Parameter references support output piping with:

`$node.<NODE_ID>.output.<path.to.value>`

- x402 events are tracked per execution.
- Idempotency key support prevents duplicate execution side effects.

## How To Create Workflows

Workflows are created with `POST /workflows`. The fields vary by `execution_type`.

### 1) Local Workflow

```bash
curl -X POST http://127.0.0.1:8000/workflows \
	-H "Content-Type: application/json" \
	-d '{
		"id": "wf-local-echo",
		"name": "Local Echo",
		"description": "Echoes input for composition",
		"metadata": {"tags": ["demo", "local"]},
		"input_schema": {"type": "object"},
		"output_schema": {"type": "object"},
		"execution_type": "local",
		"owner_id": "agent-owner",
		"visibility": "public",
		"pricing": {
			"enabled": false,
			"x402": {"price": "0", "token": "USDC", "recipient": "0x0"}
		}
	}'
```

### 2) HTTP Workflow

```bash
curl -X POST http://127.0.0.1:8000/workflows \
	-H "Content-Type: application/json" \
	-d '{
		"id": "wf-http-svc",
		"name": "External API Workflow",
		"execution_type": "http",
		"endpoint": "https://api.example.com/execute",
		"method": "POST",
		"headers": {"Authorization": "Bearer <token>"},
		"body_template": "{\"query\": \"${input.prompt}\"}",
		"owner_id": "agent-owner",
		"visibility": "public",
		"pricing": {
			"enabled": true,
			"x402": {
				"price": "1.00",
				"token": "USDC",
				"recipient": "0x0000000000000000000000000000000000000000"
			}
		}
	}'
```

### 3) KeeperHub Workflow

```bash
curl -X POST http://127.0.0.1:8000/workflows \
	-H "Content-Type: application/json" \
	-d '{
		"id": "wf-keeperhub-job",
		"name": "KeeperHub Job Runner",
		"execution_type": "keeperhub",
		"workflow_id": "keeperhub-workflow-id",
		"owner_id": "agent-owner",
		"visibility": "public",
		"pricing": {
			"enabled": true,
			"x402": {
				"price": "2.00",
				"token": "USDC",
				"recipient": "0x0000000000000000000000000000000000000000"
			}
		}
	}'
```

## How To Compose Bigger Workflows (Workflow-of-Workflows)

Use `POST /execute` with an `execution_graph` that references multiple workflow IDs.

Example: pipe node `A` output price into node `B` input.

```bash
curl -X POST http://127.0.0.1:8000/execute \
	-H "Content-Type: application/json" \
	-d '{
		"actor_id": "agent-1",
		"idempotency_key": "run-001",
		"input": {"coin": "btc"},
		"execution_graph": {
			"nodes": [
				{
					"id": "A",
					"workflow_id": "chainlink-price-fetcher",
					"params": {"coin": "btc"}
				},
				{
					"id": "B",
					"workflow_id": "email-send",
					"params": {
						"to": "alerts@example.com",
						"content": "$node.A.output.price"
					}
				}
			],
			"edges": [
				{"from_id": "A", "to_id": "B", "condition": "true"}
			]
		},
		"payment_payloads": {}
	}'
```

## Environment Variables

### Backend

- `ACTIVITY_TTL_SECONDS` (default: `60`)
- `CLIPBOARD_MAX_MESSAGES` (default: `200`)
- `HUB_STATE_PATH` (default: `data/hub_state.json`)
- `EXECUTION_STALE_SECONDS` (default: `600`)
- `X402_FACILITATOR_URL` (optional)
- `FACILITATOR_URL` (optional alias)
- `AXL_REGISTRY_URL` (optional, for register/unregister on lifecycle)
- `AXL_MCP_URL` (optional, advertised endpoint for registry)

x402 note:

- Network is enforced for `base-sepolia` only in current implementation.

### Frontend

- `VITE_NODE_BASE_URL` (default: `http://127.0.0.1:8000`)
- `VITE_PUBLIC_PEER_ID` (optional, shown in dashboard header)

## Local Run

### 1) Start Backend

```bash
cd node_backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### 2) Start Dashboard

```bash
cd dashboard
npm install
npm run dev
```

Open: `http://127.0.0.1:5173`

## Deployment Notes

- For hosted frontend + backend, use same-origin proxying (for example Vercel rewrite `/api/*` to backend host) to avoid mixed-content issues.
- Set `VITE_PUBLIC_PEER_ID` in frontend env so operators and agents can see and dial the public node identity.
- Set `AXL_REGISTRY_URL` + `AXL_MCP_URL` in backend env so the node self-registers into your AXL network entrypoint.
