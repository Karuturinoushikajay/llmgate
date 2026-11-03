"use client";

import { useEffect, useState } from "react";
import { adminFetch } from "@/lib/api";

type Usage = {
  requests: number;
  total_tokens: number;
  estimated_cost_usd: number;
  by_model: Array<{
    model: string;
    requests: number;
    total_tokens: number;
    estimated_cost_usd: number;
  }>;
};

export default function UsagePage() {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    adminFetch<Usage>("/admin/usage")
      .then(setUsage)
      .catch((err: Error) => setError(err.message));
  }, []);

  return (
    <>
      <h1>Usage</h1>
      <p className="lead">Aggregated from the append-only request log. No prompts.</p>
      {error ? <p className="error">{error}</p> : null}
      <div className="grid">
        <div className="stat">
          <div className="label">Requests</div>
          <div className="value">{usage?.requests ?? "—"}</div>
        </div>
        <div className="stat">
          <div className="label">Tokens</div>
          <div className="value">{usage?.total_tokens ?? "—"}</div>
        </div>
        <div className="stat">
          <div className="label">Est. USD</div>
          <div className="value">
            {usage ? usage.estimated_cost_usd.toFixed(4) : "—"}
          </div>
        </div>
      </div>
      <div className="panel" style={{ marginTop: "1rem" }}>
        <table>
          <thead>
            <tr>
              <th>Model</th>
              <th>Requests</th>
              <th>Tokens</th>
              <th>USD</th>
            </tr>
          </thead>
          <tbody>
            {(usage?.by_model || []).map((row) => (
              <tr key={row.model}>
                <td>{row.model}</td>
                <td>{row.requests}</td>
                <td>{row.total_tokens}</td>
                <td>{row.estimated_cost_usd.toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
