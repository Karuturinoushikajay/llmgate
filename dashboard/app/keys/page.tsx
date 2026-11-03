"use client";

import { useEffect, useState, type FormEvent } from "react";
import { adminFetch } from "@/lib/api";

type Key = {
  id: string;
  name: string;
  key_prefix: string;
  team_id: string | null;
  created_at: string;
  revoked_at: string | null;
};

type Team = { id: string; name: string };

export default function KeysPage() {
  const [keys, setKeys] = useState<Key[]>([]);
  const [teams, setTeams] = useState<Team[]>([]);
  const [name, setName] = useState("dashboard");
  const [teamId, setTeamId] = useState("");
  const [created, setCreated] = useState("");
  const [error, setError] = useState("");

  async function refresh() {
    const [keyList, teamList] = await Promise.all([
      adminFetch<{ data: Key[] }>("/admin/keys"),
      adminFetch<{ data: Team[] }>("/admin/teams"),
    ]);
    setKeys(keyList.data);
    setTeams(teamList.data);
  }

  useEffect(() => {
    refresh().catch((err: Error) => setError(err.message));
  }, []);

  async function onCreate(event: FormEvent) {
    event.preventDefault();
    setError("");
    setCreated("");
    try {
      const body: { name: string; team_id?: string } = { name };
      if (teamId) {
        body.team_id = teamId;
      }
      const row = await adminFetch<Key & { key: string }>("/admin/keys", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setCreated(row.key);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "create failed");
    }
  }

  async function onRevoke(id: string) {
    setError("");
    try {
      await adminFetch(`/admin/keys/${id}`, { method: "DELETE" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "revoke failed");
    }
  }

  return (
    <>
      <h1>API keys</h1>
      <p className="lead">Create and revoke gateway keys. Secrets are shown once.</p>
      <form className="form panel" onSubmit={onCreate}>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} required />
        </label>
        <label>
          Team
          <select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
            <option value="">None</option>
            {teams.map((team) => (
              <option key={team.id} value={team.id}>
                {team.name}
              </option>
            ))}
          </select>
        </label>
        <button type="submit">Create key</button>
        {created ? <p className="ok mono">New key: {created}</p> : null}
        {error ? <p className="error">{error}</p> : null}
      </form>
      <div className="panel" style={{ marginTop: "1rem" }}>
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Prefix</th>
              <th>Team</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => (
              <tr key={key.id}>
                <td>{key.name}</td>
                <td className="mono">{key.key_prefix}</td>
                <td className="mono">{key.team_id || "—"}</td>
                <td>
                  {key.revoked_at ? (
                    "revoked"
                  ) : (
                    <button className="danger" type="button" onClick={() => onRevoke(key.id)}>
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
