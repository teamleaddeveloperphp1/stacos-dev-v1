import React, { useState } from "react";
import {
  Block,
  BlockTitle,
  Button,
  List,
  ListInput,
  Navbar,
  Page,
  Preloader,
} from "framework7-react";

import { api } from "../api.js";

/**
 * The dual-OTP screen.
 *
 * One screen, both codes, one submission — the same contract as the web client,
 * because it is the same server-side state machine. Splitting it into two steps
 * would let one channel be satisfied and the other deferred, which is precisely
 * what the policy exists to prevent.
 */
export default function VerifyPage({ verificationId, email, f7router }) {
  const [emailCode, setEmailCode] = useState("");
  const [phoneCode, setPhoneCode] = useState("");
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setErrors({});
    setBusy(true);
    try {
      await api.verify(verificationId, emailCode, phoneCode);
      f7router.navigate("/", { clearPreviousHistory: true });
    } catch (err) {
      const payload = err.payload ?? {};
      setErrors({
        email: payload.email_error,
        phone: payload.phone_error,
        general: err.message,
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Page>
      <Navbar title="Verify it's you" backLink="Back" />

      <BlockTitle>Two codes, one step</BlockTitle>
      <Block>
        <p style={{ color: "var(--stacos-text-muted)" }}>
          We sent a code to {email} and a code to your registered mobile number.
          Enter both to continue.
        </p>
      </Block>

      <form onSubmit={submit}>
        <List strongIos insetIos>
          <ListInput
            className="otp-input"
            type="tel"
            label="Code sent to your email"
            value={emailCode}
            onInput={(e) => setEmailCode(e.target.value)}
            maxlength={6}
            inputmode="numeric"
            autocomplete="one-time-code"
            errorMessage={errors.email}
            errorMessageForce={Boolean(errors.email)}
            required
          />
          <ListInput
            className="otp-input"
            type="tel"
            label="Code sent to your phone"
            value={phoneCode}
            onInput={(e) => setPhoneCode(e.target.value)}
            maxlength={6}
            inputmode="numeric"
            autocomplete="one-time-code"
            errorMessage={errors.phone}
            errorMessageForce={Boolean(errors.phone)}
            required
          />
        </List>

        {errors.general && (
          <Block>
            <p style={{ color: "var(--stacos-status-overdue)" }} role="alert">
              {errors.general}
            </p>
          </Block>
        )}

        <Block>
          <Button large fill type="submit" disabled={busy}>
            {busy ? <Preloader color="white" size={20} /> : "Verify and continue"}
          </Button>
        </Block>
      </form>
    </Page>
  );
}
