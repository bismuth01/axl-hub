import { useEffect, useMemo, useState } from "react";

function normalizeBaseUrl(value) {
  const fallback = import.meta.env.PROD ? "/api" : "http://127.0.0.1:8000";
  const raw = String(value || fallback).trim().replace(/\/+$/, "");

  if (!raw) {
    return fallback;
  }

  if (raw.startsWith("/")) {
    return raw;
  }

  if (/^https?:\/\//i.test(raw)) {
    if (import.meta.env.PROD && raw.startsWith("http://")) {
      return "/api";
    }
    return raw;
  }

  if (raw.startsWith("//")) {
    const protocol = typeof window !== "undefined" ? window.location.protocol : "http:";
    return `${protocol}${raw}`;
  }

  return import.meta.env.PROD ? "/api" : `http://${raw}`;
}

const BASE_URL = normalizeBaseUrl(import.meta.env.VITE_NODE_BASE_URL);
const PUBLIC_PEER_ID = import.meta.env.VITE_PUBLIC_PEER_ID || "";
const REFRESH_INTERVAL_MS = 2500;

function fmtTimestamp(value) {
  if (!value) {
    return "-";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString();
}

function parseJson(text, fallback) {
  if (!text || !text.trim()) {
    return fallback;
  }

  return JSON.parse(text);
}

function prettyJson(value) {
  return JSON.stringify(value, null, 2);
}

function buildDemoGraph(workflows) {
  const [primary, fallback] = workflows;
  return prettyJson({
    nodes: [
      {
        id: "A",
        workflow_id: primary?.id || "wf-primary",
        params: { seed: 1 },
        on_failure: "B",
      },
      {
        id: "B",
        workflow_id: fallback?.id || primary?.id || "wf-fallback",
        params: { result: "fallback-ok" },
      },
    ],
    edges: [{ from_id: "A", to_id: "B", condition: "true" }],
  });
}

function App() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [topology, setTopology] = useState({ participants: [] });
  const [publicWorkflows, setPublicWorkflows] = useState([]);
  const [clipboard, setClipboard] = useState({ max_messages: 0, messages: [] });
  const [executionDetails, setExecutionDetails] = useState(null);
  const [latestExecutionId, setLatestExecutionId] = useState("");
  const [workflowSubmitting, setWorkflowSubmitting] = useState(false);
  const [executionSubmitting, setExecutionSubmitting] = useState(false);

  const [workflowForm, setWorkflowForm] = useState({
    id: "wf-demo",
    name: "Demo Workflow",
    description: "A reusable atomic capability",
    metadata: "{\n  \"tags\": [\"demo\", \"paid\"],\n  \"category\": \"ops\"\n}",
    input_schema: "{\n  \"type\": \"object\",\n  \"properties\": {\n    \"seed\": {\n      \"type\": \"number\"\n    }\n  }\n}",
    output_schema: "{\n  \"type\": \"object\",\n  \"properties\": {\n    \"result\": {\n      \"type\": \"string\"\n    }\n  }\n}",
    execution_type: "local",
    endpoint: "",
    method: "POST",
    headers: "",
    body_template: "",
    workflow_id: "",
    owner_id: "agent-owner",
    visibility: "public",
    pricing_enabled: true,
    price: "1.00",
    token: "USDC",
    recipient: "0x0000000000000000000000000000000000000000",
  });

  const [workflowFormErrors, setWorkflowFormErrors] = useState({});

  const [executionForm, setExecutionForm] = useState({
    actor_id: "agent-1",
    idempotency_key: "",
    input: "{\n  \"seed\": 1\n}",
    execution_graph: "",
    payment_payloads: "{}",
  });

  useEffect(() => {
    let mounted = true;

    const load = async () => {
      try {
        const [topologyRes, clipboardRes, workflowsRes] = await Promise.all([
          fetch(`${BASE_URL}/topology`),
          fetch(`${BASE_URL}/clipboard`),
          fetch(`${BASE_URL}/workflows/public`),
        ]);

        if (!topologyRes.ok || !clipboardRes.ok || !workflowsRes.ok) {
          throw new Error("One or more API calls failed");
        }

        const [topologyData, clipboardData, workflowsData] = await Promise.all([
          topologyRes.json(),
          clipboardRes.json(),
          workflowsRes.json(),
        ]);

        if (!mounted) {
          return;
        }

        setTopology(topologyData);
        setClipboard(clipboardData);
        setPublicWorkflows(workflowsData.workflows || []);
        setError("");
      } catch (err) {
        if (!mounted) {
          return;
        }
        setError(err instanceof Error ? err.message : "Unknown error while loading monitor data");
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    };

    load();
    const timer = setInterval(load, REFRESH_INTERVAL_MS);

    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, []);

  

  const workflowCount = publicWorkflows.length;
  const publicSuccessAverage = useMemo(() => {
    if (publicWorkflows.length === 0) {
      return 0;
    }

    const total = publicWorkflows.reduce((sum, workflow) => sum + (workflow.success_rate || 0), 0);
    return total / publicWorkflows.length;
  }, [publicWorkflows]);

  // Validation function for workflow form based on execution_type
  const validateWorkflowForm = () => {
    const errors = {};
    const { execution_type, id, name, endpoint, workflow_id } = workflowForm;

    if (!id || !id.trim()) {
      errors.id = "Workflow ID is required";
    }
    if (!name || !name.trim()) {
      errors.name = "Name is required";
    }

    if (execution_type === "http") {
      if (!endpoint || !endpoint.trim()) {
        errors.endpoint = "Endpoint is required for HTTP workflows";
      }
    } else if (execution_type === "keeperhub") {
      if (!workflow_id || !workflow_id.trim()) {
        errors.workflow_id = "Workflow ID is required for KeeperHub workflows";
      }
    }

    setWorkflowFormErrors(errors);
    return Object.keys(errors).length === 0;
  };

  // Clean up irrelevant fields when execution_type changes
  const handleExecutionTypeChange = (newType) => {
    const cleaned = { ...workflowForm, execution_type: newType };

    if (newType === "local") {
      cleaned.endpoint = "";
      cleaned.method = "POST";
      cleaned.headers = "";
      cleaned.body_template = "";
      cleaned.workflow_id = "";
    } else if (newType === "http") {
      cleaned.workflow_id = "";
    } else if (newType === "keeperhub") {
      cleaned.endpoint = "";
      cleaned.method = "POST";
      cleaned.headers = "";
      cleaned.body_template = "";
    }

    setWorkflowForm(cleaned);
    setWorkflowFormErrors({});
  };

  const handleCreateWorkflow = async (event) => {
    event.preventDefault();

    // Validate before submitting
    if (!validateWorkflowForm()) {
      return;
    }

    setWorkflowSubmitting(true);
    setError("");

    try {
      const payload = {
        id: workflowForm.id.trim(),
        name: workflowForm.name.trim(),
        description: workflowForm.description.trim() || null,
        metadata: parseJson(workflowForm.metadata, {}),
        input_schema: parseJson(workflowForm.input_schema, {}),
        output_schema: parseJson(workflowForm.output_schema, {}),
        execution_type: workflowForm.execution_type,
        owner_id: workflowForm.owner_id.trim(),
        visibility: workflowForm.visibility,
        pricing: {
          enabled: workflowForm.pricing_enabled,
          x402: {
            price: workflowForm.price,
            token: workflowForm.token,
            recipient: workflowForm.recipient,
          },
        },
      };

      // Shape payload based on execution_type
      if (workflowForm.execution_type === "http") {
        payload.endpoint = workflowForm.endpoint.trim();
        if (workflowForm.method) payload.method = workflowForm.method;
        if (workflowForm.headers?.trim()) {
          payload.headers = parseJson(workflowForm.headers, {});
        }
        if (workflowForm.body_template?.trim()) {
          payload.body_template = workflowForm.body_template.trim();
        }
      } else if (workflowForm.execution_type === "keeperhub") {
        payload.workflow_id = workflowForm.workflow_id.trim();
      }
      // local workflows: no execution-specific fields needed

      const response = await fetch(`${BASE_URL}/workflows`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        throw new Error(await response.text());
      }

      const created = await response.json();
      setPublicWorkflows((current) => [{ ...created, usage_count: 0, success_rate: 0 }, ...current]);
      
      // Reset form after successful creation
      setWorkflowForm({
        id: "",
        name: "",
        description: "",
        metadata: "{}",
        input_schema: "{}",
        output_schema: "{}",
        execution_type: "local",
        endpoint: "",
        method: "POST",
        headers: "",
        body_template: "",
        workflow_id: "",
        owner_id: "agent-owner",
        visibility: "public",
        pricing_enabled: true,
        price: "1.00",
        token: "USDC",
        recipient: "0x0000000000000000000000000000000000000000",
      });
      setWorkflowFormErrors({});
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create workflow");
    } finally {
      setWorkflowSubmitting(false);
    }
  };

  const handleExecute = async (event) => {
    event.preventDefault();
    setExecutionSubmitting(true);
    setError("");

    try {
      const graph = parseJson(executionForm.execution_graph, null);
      if (!graph) {
        throw new Error("Execution graph is required");
      }

      const payload = {
        actor_id: executionForm.actor_id.trim() || null,
        idempotency_key: executionForm.idempotency_key.trim() || null,
        execution_graph: graph,
        input: parseJson(executionForm.input, {}),
        payment_payloads: parseJson(executionForm.payment_payloads, {}),
      };

      const response = await fetch(`${BASE_URL}/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const responseData = await response.json();
      if (!response.ok) {
        throw new Error(responseData.detail || JSON.stringify(responseData));
      }

      setLatestExecutionId(responseData.execution_id);
      const executionRes = await fetch(`${BASE_URL}/executions/${responseData.execution_id}`);
      if (executionRes.ok) {
        setExecutionDetails(await executionRes.json());
      } else {
        setExecutionDetails({
          id: responseData.execution_id,
          status: responseData.status,
          output: responseData.outputs || {},
          logs: [],
          payment: [],
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to execute graph");
    } finally {
      setExecutionSubmitting(false);
    }
  };

  const loadDemoGraph = () => {
    setExecutionForm((current) => ({
      ...current,
      execution_graph: buildDemoGraph(publicWorkflows),
    }));
  };

  return (
    <div className="app-shell">
      <header className="hero">
        <p className="eyebrow">AXL HUB DEMO CONSOLE</p>
        <h1>Agent-first execution, discovery, and monetization</h1>
        <p className="subtitle">Monitoring node endpoints at {BASE_URL}</p>
        {PUBLIC_PEER_ID && (
          <p className="subtitle">Public Peer: {PUBLIC_PEER_ID}</p>
        )}
      </header>

      <section className="stats-grid">
        <article className="stat-card">
          <h2>Participants</h2>
          <strong>{topology.participant_count || 0}</strong>
        </article>
        <article className="stat-card">
          <h2>Public Workflows</h2>
          <strong>{workflowCount}</strong>
        </article>
        
        <article className="stat-card">
          <h2>Average Success Rate</h2>
          <strong>{Math.round(publicSuccessAverage * 100)}%</strong>
        </article>
      </section>

      {loading && <p className="status">Loading console data...</p>}
      {error && <p className="status error">{error}</p>}

      <main className="panel-grid">
        <section className="panel panel-wide">
          <div className="panel-header">
            <div>
              <h2>Discovery</h2>
              <p className="panel-subtitle">Public workflows ranked by basic reliability.</p>
            </div>
          </div>
          <div className="workflow-list">
            {publicWorkflows.map((workflow) => (
              <article key={workflow.id} className="workflow-card">
                <header>
                  <div>
                    <h3>{workflow.name}</h3>
                    <p className="mono">{workflow.id}</p>
                  </div>
                  <span className="pill neutral">{workflow.visibility}</span>
                </header>
                <p>{workflow.description || "No description"}</p>
                <div className="workflow-meta">
                  <span>Usage: {workflow.usage_count || 0}</span>
                  <span>Success: {Math.round((workflow.success_rate || 0) * 100)}%</span>
                  <span>
                    Pricing: {workflow.pricing?.enabled ? `${workflow.pricing.x402?.price || "?"} ${workflow.pricing.x402?.token || "TOKEN"}` : "free"}
                  </span>
                </div>
                {workflow.metadata && Object.keys(workflow.metadata).length > 0 && (
                  <pre className="code-block">{prettyJson(workflow.metadata)}</pre>
                )}
              </article>
            ))}
            {publicWorkflows.length === 0 && <p>No public workflows yet.</p>}
          </div>
        </section>

        <section className="panel">
          <h2>Create Workflow</h2>
          {Object.keys(workflowFormErrors).length > 0 && (
            <div className="error-summary">
              <p>
                <strong>Please fix the following errors:</strong>
              </p>
              <ul>
                {Object.entries(workflowFormErrors).map(([field, message]) => (
                  <li key={field}>
                    {field}: {message}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <form className="form-grid" onSubmit={handleCreateWorkflow}>
            <label className="field">
              <span>Workflow ID</span>
              <input value={workflowForm.id} onChange={(e) => setWorkflowForm({ ...workflowForm, id: e.target.value })} />
              {workflowFormErrors.id && <span className="field-error">{workflowFormErrors.id}</span>}
            </label>
            <label className="field">
              <span>Name</span>
              <input value={workflowForm.name} onChange={(e) => setWorkflowForm({ ...workflowForm, name: e.target.value })} />
              {workflowFormErrors.name && <span className="field-error">{workflowFormErrors.name}</span>}
            </label>
            <label className="field field-wide">
              <span>Description</span>
              <input value={workflowForm.description} onChange={(e) => setWorkflowForm({ ...workflowForm, description: e.target.value })} />
            </label>
            <label className="field">
              <span>Owner ID</span>
              <input value={workflowForm.owner_id} onChange={(e) => setWorkflowForm({ ...workflowForm, owner_id: e.target.value })} />
            </label>
            <label className="field">
              <span>Visibility</span>
              <select value={workflowForm.visibility} onChange={(e) => setWorkflowForm({ ...workflowForm, visibility: e.target.value })}>
                <option value="public">public</option>
                <option value="private">private</option>
              </select>
            </label>
            <label className="field">
              <span>Execution Type</span>
              <select value={workflowForm.execution_type} onChange={(e) => handleExecutionTypeChange(e.target.value)}>
                <option value="local">local (echo, no external calls)</option>
                <option value="http">http (external REST endpoint)</option>
                <option value="keeperhub">keeperhub (on-chain execution)</option>
              </select>
            </label>

            {/* HTTP-specific fields */}
            {workflowForm.execution_type === "http" && (
              <>
                <label className="field">
                  <span>Endpoint *</span>
                  <input
                    value={workflowForm.endpoint}
                    onChange={(e) => setWorkflowForm({ ...workflowForm, endpoint: e.target.value })}
                    placeholder="https://api.example.com/execute"
                  />
                  {workflowFormErrors.endpoint && <span className="field-error">{workflowFormErrors.endpoint}</span>}
                </label>
                <label className="field">
                  <span>HTTP Method</span>
                  <select value={workflowForm.method} onChange={(e) => setWorkflowForm({ ...workflowForm, method: e.target.value })}>
                    <option value="GET">GET</option>
                    <option value="POST">POST</option>
                    <option value="PUT">PUT</option>
                    <option value="PATCH">PATCH</option>
                  </select>
                </label>
                <label className="field field-wide">
                  <span>Headers (optional JSON)</span>
                  <textarea
                    rows="3"
                    value={workflowForm.headers}
                    onChange={(e) => setWorkflowForm({ ...workflowForm, headers: e.target.value })}
                    placeholder='{"Authorization": "Bearer token", "X-Custom": "value"}'
                  />
                </label>
                <label className="field field-wide">
                  <span>Body Template (optional)</span>
                  <textarea
                    rows="3"
                    value={workflowForm.body_template}
                    onChange={(e) => setWorkflowForm({ ...workflowForm, body_template: e.target.value })}
                    placeholder='{"query": "${input.seed}", "format": "json"}'
                  />
                </label>
              </>
            )}

            {/* KeeperHub-specific fields */}
            {workflowForm.execution_type === "keeperhub" && (
              <>
                <label className="field">
                  <span>Workflow ID / Job Identifier *</span>
                  <input
                    value={workflowForm.workflow_id}
                    onChange={(e) => setWorkflowForm({ ...workflowForm, workflow_id: e.target.value })}
                    placeholder="keeperhub-workflow-id"
                  />
                  {workflowFormErrors.workflow_id && <span className="field-error">{workflowFormErrors.workflow_id}</span>}
                </label>
              </>
            )}

            <label className="field field-wide">
              <span>Metadata JSON</span>
              <textarea rows="4" value={workflowForm.metadata} onChange={(e) => setWorkflowForm({ ...workflowForm, metadata: e.target.value })} />
            </label>
            <label className="field field-wide">
              <span>Input Schema JSON</span>
              <textarea rows="5" value={workflowForm.input_schema} onChange={(e) => setWorkflowForm({ ...workflowForm, input_schema: e.target.value })} />
            </label>
            <label className="field field-wide">
              <span>Output Schema JSON</span>
              <textarea rows="5" value={workflowForm.output_schema} onChange={(e) => setWorkflowForm({ ...workflowForm, output_schema: e.target.value })} />
            </label>
            <div className="field-inline">
              <label className="checkbox-row">
                <input type="checkbox" checked={workflowForm.pricing_enabled} onChange={(e) => setWorkflowForm({ ...workflowForm, pricing_enabled: e.target.checked })} />
                <span>Enable x402 pricing</span>
              </label>
            </div>
            <label className="field">
              <span>Price</span>
              <input value={workflowForm.price} onChange={(e) => setWorkflowForm({ ...workflowForm, price: e.target.value })} />
            </label>
            <label className="field">
              <span>Token</span>
              <input value={workflowForm.token} onChange={(e) => setWorkflowForm({ ...workflowForm, token: e.target.value })} />
            </label>
            <label className="field field-wide">
              <span>Recipient</span>
              <input value={workflowForm.recipient} onChange={(e) => setWorkflowForm({ ...workflowForm, recipient: e.target.value })} />
            </label>
            <button
              className="primary-button"
              type="submit"
              disabled={workflowSubmitting || Object.keys(workflowFormErrors).length > 0}
            >
              {workflowSubmitting ? "Creating..." : "Create Workflow"}
            </button>
          </form>
        </section>

        <section className="panel">
          <h2>Execute Graph</h2>
          <div className="toolbar">
            <button className="secondary-button" type="button" onClick={loadDemoGraph}>
              Load Demo Scenario
            </button>
          </div>
          <form className="form-grid" onSubmit={handleExecute}>
            <label className="field">
              <span>Actor ID</span>
              <input value={executionForm.actor_id} onChange={(e) => setExecutionForm({ ...executionForm, actor_id: e.target.value })} />
            </label>
            <label className="field">
              <span>Idempotency Key</span>
              <input value={executionForm.idempotency_key} onChange={(e) => setExecutionForm({ ...executionForm, idempotency_key: e.target.value })} />
            </label>
            <label className="field field-wide">
              <span>Execution Graph JSON</span>
              <textarea rows="14" value={executionForm.execution_graph} onChange={(e) => setExecutionForm({ ...executionForm, execution_graph: e.target.value })} placeholder={buildDemoGraph(publicWorkflows)} />
            </label>
            <label className="field field-wide">
              <span>Input JSON</span>
              <textarea rows="5" value={executionForm.input} onChange={(e) => setExecutionForm({ ...executionForm, input: e.target.value })} />
            </label>
              <label className="field field-wide">
                <span>Payment Payloads JSON</span>
                <textarea rows="4" value={executionForm.payment_payloads} onChange={(e) => setExecutionForm({ ...executionForm, payment_payloads: e.target.value })} placeholder={`{"A": {...}, "B": {...}}`} />
              </label>
            <button className="primary-button" type="submit" disabled={executionSubmitting}>
              {executionSubmitting ? "Executing..." : "Execute Graph"}
            </button>
          </form>
        </section>

        <section className="panel panel-wide">
          <div className="panel-header">
            <div>
              <h2>Latest Execution</h2>
              <p className="panel-subtitle">Logs, payments, and outputs for the last submitted graph.</p>
            </div>
            {latestExecutionId && <span className="mono">{latestExecutionId}</span>}
          </div>
          {executionDetails ? (
            <div className="execution-detail">
              <div className="execution-summary">
                <article className="summary-card">
                  <span>Status</span>
                  <strong>{executionDetails.status}</strong>
                </article>
                <article className="summary-card">
                  <span>Created</span>
                  <strong>{fmtTimestamp(executionDetails.created_at)}</strong>
                </article>
                <article className="summary-card">
                  <span>Finished</span>
                  <strong>{fmtTimestamp(executionDetails.completed_at)}</strong>
                </article>
                <article className="summary-card">
                  <span>Payments</span>
                  <strong>{(executionDetails.payment || []).length}</strong>
                </article>
              </div>
              <div className="execution-columns">
                <div>
                  <h3>Logs</h3>
                  <pre className="code-block tall">{prettyJson(executionDetails.logs || [])}</pre>
                </div>
                <div>
                  <h3>Payments</h3>
                  <pre className="code-block tall">{prettyJson(executionDetails.payment || [])}</pre>
                </div>
              </div>
              <div>
                <h3>Outputs</h3>
                <pre className="code-block">{prettyJson(executionDetails.output || executionDetails.outputs || {})}</pre>
              </div>
            </div>
          ) : (
            <p>No execution submitted yet.</p>
          )}
        </section>

        <section className="panel">
          <h2>Topology Participants</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Label</th>
                  <th>Status</th>
                  <th>Last Seen</th>
                </tr>
              </thead>
              <tbody>
                {(topology.participants || []).map((participant) => (
                  <tr key={participant.id}>
                    <td className="mono">{participant.id}</td>
                    <td>{participant.label || "-"}</td>
                    <td>
                      <span className={participant.active ? "pill active" : "pill idle"}>
                        {participant.active ? "active" : "idle"}
                      </span>
                    </td>
                    <td>{fmtTimestamp(participant.last_seen_at)}</td>
                  </tr>
                ))}
                {(!topology.participants || topology.participants.length === 0) && (
                  <tr>
                    <td colSpan="4">No participants yet.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        

        <section className="panel panel-wide">
          <h2>Clipboard</h2>
          <div className="clipboard-list">
            {(clipboard.messages || []).map((entry) => (
              <article key={entry.id} className="clipboard-entry">
                <header>
                  <span className="mono">{entry.author_id || "unknown"}</span>
                  <time>{fmtTimestamp(entry.created_at)}</time>
                </header>
                <p>{entry.message}</p>
              </article>
            ))}
            {(!clipboard.messages || clipboard.messages.length === 0) && <p>Clipboard is empty.</p>}
          </div>
        </section>
      </main>
    </div>
  );
}

export default App;
