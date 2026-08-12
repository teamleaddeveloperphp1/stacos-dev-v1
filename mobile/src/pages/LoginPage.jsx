import React, { useState } from "react";
import {
  Block,
  Button,
  List,
  ListInput,
  Navbar,
  Page,
  Preloader,
} from "framework7-react";

import { api } from "../api.js";

export default function LoginPage({ f7router }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      // Returns a verification id, never a token. Both codes are still required.
      const { verification_id } = await api.login(email, password);
      f7router.navigate("/verify/", {
        props: { verificationId: verification_id, email },
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Page loginScreen>
      <Navbar title="STACOS" large />
      <Block>
        <p style={{ color: "var(--stacos-text-muted)" }}>
          Sign in to see what is due, approve what is waiting on you, and answer
          your accountant.
        </p>
      </Block>

      <form onSubmit={submit}>
        <List strongIos insetIos>
          <ListInput
            type="email"
            label="Email"
            placeholder="you@company.com"
            value={email}
            onInput={(e) => setEmail(e.target.value)}
            required
            validate
            autocomplete="username"
          />
          <ListInput
            type="password"
            label="Password"
            value={password}
            onInput={(e) => setPassword(e.target.value)}
            required
            autocomplete="current-password"
          />
        </List>

        {error && (
          <Block>
            <p style={{ color: "var(--stacos-status-overdue)" }} role="alert">
              {error}
            </p>
          </Block>
        )}

        <Block>
          <Button large fill type="submit" disabled={busy}>
            {busy ? <Preloader color="white" size={20} /> : "Continue"}
          </Button>
        </Block>
      </form>

      <Block>
        <p style={{ fontSize: 13, color: "var(--stacos-text-muted)" }}>
          After your password we will send a code to your email and a code to
          your phone. You will need both.
        </p>
      </Block>
    </Page>
  );
}
