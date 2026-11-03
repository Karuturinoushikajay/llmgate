"use client";

import { useEffect, useState, type FormEvent } from "react";
import { adminFetch } from "@/lib/api";

type Team = {
  id: string;
  name: string;
  monthly_token_budget: number | null;
  created_at: string;
};

export default function TeamsPage() {
  const [teams, setTeams] = useState<Team[]>([]);
  const [name, setName] = useState("");
  const [budget, setBudget] = useState("");
  const [error, setError] = useState("");

  async function refresh() {
    const listed = await adminFetch<{ data: Team[] }>("/admin/teams");
    setTeams(listed.data);
  }

  useEffect(() => {
    refresh().catch((err: Error) => setError(err.message));
  }, []);

  async function onCreate(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await adminFetch("/admin/teams", {
        method: "POST",
        body: JSON.stringify({
          name,
          monthly_token_budget: budget ? Number(budget) : null,
        }),
      });
      setName("");
      setBudget("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "create failed");
    }
  }

  return (
    <>
      <h1>Teams</h1>
      <p className="lead">Optional monthly token budgets for groups of keys.</p>
      <form className="form panel" onSubmit={onCreate}>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} required />
        </label>
        <label>
          Monthly token budget
          <input
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
            placeholder="leave blank for unlimited"
          />
        </label>
        <button type="submit">Create team</button>
        {error ? <p className="error">{error}</p> : null}
      </form>
      <div className="panel" style={{ marginTop: "1rem" }}>
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Budget</th>
              <th>Id</th>
            </tr>
          </thead>
          <tbody>
            {teams.map((team) => (
              <tr key={team.id}>
                <td>{team.name}</td>
                <td>{team.monthly_token_budget ?? "unlimited"}</td>
                <td className="mono">{team.id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
