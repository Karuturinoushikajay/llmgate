"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { adminFetch, setToken } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [token, setLocal] = useState("");
  const [error, setError] = useState("");

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setToken(token.trim());
    try {
      await adminFetch<{ role: string }>("/admin/me");
      router.push("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    }
  }

  return (
    <>
      <h1>Sign in</h1>
      <p className="lead">Paste the master key or a dashboard user token.</p>
      <form className="form panel" onSubmit={onSubmit}>
        <label>
          Bearer token
          <input
            value={token}
            onChange={(event) => setLocal(event.target.value)}
            autoComplete="off"
            required
          />
        </label>
        <button type="submit">Continue</button>
        {error ? <p className="error">{error}</p> : null}
      </form>
    </>
  );
}
