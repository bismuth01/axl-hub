# AXL Hub Node (FastAPI)

A lightweight in-memory node that models AXL-style hub behavior.

State is persisted to a JSON file on disk so participants, workstations, access state, and clipboard entries survive restarts.

## Features

- `GET /topology`: Returns current participants and activity summary.
- `CRUD /topology/participants`: Manage participants.
- `CRUD /workstations`: Manage workstations.
- `CRUD /workflows`: Manage atomic capabilities.
- `GET /workflows/public`: Public workflow discovery with pricing, usage count, and success rate.
- `POST /execute`: Agent-first execution entrypoint for workflow IDs or execution graphs.
- `POST /workstations/{id}/access`: Touch workstation access for a participant.
- `POST /workstations/{id}/execute`: Execute the workstation's registered workflow payload.
- `GET /workstations/{id}/executions`: Inspect execution history for a workstation.
- `GET/POST /clipboard`: Shared ring-buffer message feed.

Each workstation carries an arbitrary `workflow` object and an `owner_id`.

- The owner can create, update, delete, and inspect the private workflow and execution history.
- Other agents can only discover the workstation summary and invoke it through `POST /workstations/{id}/execute`.
- Public workstation reads do not expose the workflow or execution details.

### Demo-oriented execution model

- Workflows are treated as black-box capabilities with `input_schema`, `output_schema`, and pricing metadata.
- `POST /execute` accepts `workflow_id` or an `execution_graph` and executes deterministically in topological order.
- Output references like `$node.A.output.value` are resolved between nodes.
- Paid nodes reserve payment before execution, settle on success, and release on failure.
- If `X402_FACILITATOR_URL` is configured, the node calls the facilitator's `/verify` and `/settle` endpoints.
- `GET /executions/{execution_id}` returns logs, outputs, and payment events for the dashboard.

## Runtime Config

Set with environment variables:

- `ACTIVITY_TTL_SECONDS` (default: `60`)
- `CLIPBOARD_MAX_MESSAGES` (default: `200`)
- `HUB_STATE_PATH` (default: `data/hub_state.json`)
- `X402_FACILITATOR_URL` (optional: off-chain facilitator URL for x402 verification / settlement)
- `FACILITATOR_URL` (same as above; use this if your deployment platform prefers a shorter env var name)
- x402 payments are enforced for `base-sepolia` only
- `EXECUTION_STALE_SECONDS` (default: `600`)
 
Additional optional environment variables for integration:

- `AXL_REGISTRY_URL` (optional): If set, the node will attempt to register a small set of service endpoints with the registry on startup and unregister them on shutdown. Example: `http://127.0.0.1:9003`.
- `AXL_MCP_URL` (optional): The public MCP/endpoint URL that will be advertised to the registry for each service. Example: `https://my-node.example.com`.
## Run

```bash
cd node_backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Node base URL: `http://127.0.0.1:8000`
