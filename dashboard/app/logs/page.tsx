"use client";

import { useEffect, useState } from "react";
import { adminFetch } from "@/lib/api";

type Log = {
  id: string;
  request_id: string;
  model: string | null;
  provider: string | null;
  cache: string;
  status_code: number;
  latency_ms: number;
  total_tokens: number | null;
  estimated_cost_usd: number | null;
  created_at: string;
};

type Audit = {
  id: string;
  actor: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  created_at: string;
};

export default function LogsPage() {
  const [logs, setLogs] = useState<Log[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([
      adminFetch<{ data: Log[] }>("/admin/logs?limit=50"),
      adminFetch<{ data: Audit[] }>("/admin/audit?limit=50"),
    ])
      .then(([logList, auditList]) => {
        setLogs(logList.data);
        setAudit(auditList.data);
      })
      .catch((err: Error) => setError(err.message));
  }, []);

  return (
    <>
      <h1>Logs and audit</h1>
      <p className="lead">Request rows are chat traffic. Audit rows are admin changes.</p>
      {error ? <p className="error">{error}</p> : null}
      <h2>Requests</h2>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Model</th>
              <th>Cache</th>
              <th>Status</th>
              <th>ms</th>
              <th>Tokens</th>
              <th>USD</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((row) => (
              <tr key={row.id}>
                <td className="mono">{row.created_at}</td>
                <td>{row.model}</td>
                <td>{row.cache}</td>
                <td>{row.status_code}</td>
                <td>{row.latency_ms}</td>
                <td>{row.total_tokens ?? "—"}</td>
                <td>{row.estimated_cost_usd?.toFixed(4) ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>Audit</h2>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Actor</th>
              <th>Action</th>
              <th>Resource</th>
            </tr>
          </thead>
          <tbody>
            {audit.map((row) => (
              <tr key={row.id}>
                <td className="mono">{row.created_at}</td>
                <td className="mono">{row.actor}</td>
                <td>
                  {row.action} {row.resource_type}
                </td>
                <td className="mono">{row.resource_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
