"use client";

import { useEffect, useState } from "react";
import { adminFetch, clearToken } from "@/lib/api";

type Me = { actor: string; role: string; is_master: boolean };
type Usage = {
  requests: number;
  total_tokens: number;
  estimated_cost_usd: number;
};

export default function HomePage() {
  const [me, setMe] = useState<Me | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([adminFetch<Me>("/admin/me"), adminFetch<Usage>("/admin/usage")])
      .then(([who, stats]) => {
        setMe(who);
        setUsage(stats);
      })
      .catch((err: Error) => setError(err.message));
  }, []);

  return (
    <>
      <h1>Overview</h1>
      <p className="lead">Keys, teams, budgets, routing, and usage for this gateway.</p>
      {error ? (
        <p className="error">
          {error}. <a href="/login">Sign in</a>
        </p>
      ) : null}
      {me ? (
        <p className="ok mono">
          Signed in as {me.is_master ? "master" : me.actor} ({me.role}){" "}
          <button className="secondary" type="button" onClick={() => clearToken()}>
            Sign out
          </button>
        </p>
      ) : null}
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
    </>
  );
}
