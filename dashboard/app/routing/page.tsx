"use client";

import { useEffect, useState } from "react";
import { adminFetch } from "@/lib/api";

type Routing = {
  route_simple_chars: number;
  models: Array<{
    id: string;
    provider: string;
    upstream_model: string;
    fallbacks: string[];
    routes: { simple: string; complex: string } | null;
  }>;
  pricing: Record<string, { prompt: number; completion: number }>;
};

export default function RoutingPage() {
  const [data, setData] = useState<Routing | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    adminFetch<Routing>("/admin/routing")
      .then(setData)
      .catch((err: Error) => setError(err.message));
  }, []);

  return (
    <>
      <h1>Routing rules</h1>
      <p className="lead">
        Loaded from models.yaml. Simple character ceiling: {data?.route_simple_chars ?? "—"}.
        Editing the file still requires a restart.
      </p>
      {error ? <p className="error">{error}</p> : null}
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>Id</th>
              <th>Provider</th>
              <th>Upstream</th>
              <th>Fallbacks</th>
              <th>Routes</th>
            </tr>
          </thead>
          <tbody>
            {(data?.models || []).map((model) => (
              <tr key={model.id}>
                <td>{model.id}</td>
                <td>{model.provider}</td>
                <td className="mono">{model.upstream_model}</td>
                <td>{model.fallbacks.join(", ") || "—"}</td>
                <td>
                  {model.routes
                    ? `${model.routes.simple} / ${model.routes.complex}`
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
