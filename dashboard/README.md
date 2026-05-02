# AXL Hub Dashboard (React)

A separate monitoring app that polls the node API on localhost.

It now also serves as a minimal operator console for the demo flow.
## Config

- `VITE_NODE_BASE_URL` (default: `http://127.0.0.1:8000`)

## Run

```bash
cd dashboard
npm install
npm run dev
```

Open: `http://127.0.0.1:5173`

## Demo UI

- Create workflows with pricing metadata.
- Paste or generate an execution graph.
- Launch an execution and inspect the latest logs, payments, and outputs.
- View public workflow discovery with usage count and basic success rate.
