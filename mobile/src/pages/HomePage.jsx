import React, { useEffect, useState } from "react";
import {
  Block,
  BlockTitle,
  Button,
  List,
  ListItem,
  Navbar,
  Page,
  Preloader,
} from "framework7-react";

import { api } from "../api.js";

/**
 * Signed-in shell.
 *
 * Shows who you are and which organisations you can switch between — the mobile
 * equivalent of the web client's tenant switcher. The feature screens (today's
 * compliance, approvals, information requests, notices, evidence capture) arrive
 * with the modules that own those objects; putting placeholder versions here
 * would mean two implementations to keep in step.
 */
export default function HomePage({ f7router }) {
  const [me, setMe] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .me()
      .then(setMe)
      .catch((err) => setError(err.message));
  }, []);

  async function signOut() {
    await api.logout();
    f7router.navigate("/login/", { clearPreviousHistory: true });
  }

  if (error) {
    return (
      <Page>
        <Navbar title="STACOS" large />
        <Block>
          <p style={{ color: "var(--stacos-status-overdue)" }} role="alert">
            {error}
          </p>
          <Button fill onClick={signOut}>
            Sign in again
          </Button>
        </Block>
      </Page>
    );
  }

  if (!me) {
    return (
      <Page>
        <Navbar title="STACOS" large />
        <Block className="text-align-center">
          <Preloader />
        </Block>
      </Page>
    );
  }

  return (
    <Page>
      <Navbar title="STACOS" large />

      <BlockTitle>Signed in</BlockTitle>
      <List strongIos insetIos>
        <ListItem title="Name" after={me.full_name || "—"} />
        <ListItem title="Email" after={me.email} />
        <ListItem
          title="Verification"
          after={me.verified ? "Both channels confirmed" : "Incomplete"}
        />
      </List>

      <BlockTitle>Organisations</BlockTitle>
      <List strongIos insetIos>
        {me.tenants.length === 0 && (
          <ListItem title="No organisations yet" />
        )}
        {me.tenants.map((tenant) => (
          <ListItem key={tenant.id} title={tenant.name} footer={tenant.role} after={tenant.type} />
        ))}
      </List>

      <Block>
        <p style={{ fontSize: 13, color: "var(--stacos-text-muted)" }}>
          Today's compliance view, approvals, information requests and evidence
          capture arrive with the compliance calendar.
        </p>
        <Button large onClick={signOut}>
          Sign out
        </Button>
      </Block>
    </Page>
  );
}
