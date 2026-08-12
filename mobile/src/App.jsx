import React, { useEffect, useState } from "react";
import { App as F7App, View } from "framework7-react";

import { api, loadTokens, setSignOutHandler } from "./api.js";
import LoginPage from "./pages/LoginPage.jsx";
import VerifyPage from "./pages/VerifyPage.jsx";
import HomePage from "./pages/HomePage.jsx";

const routes = [
  { path: "/", component: HomePage },
  { path: "/login/", component: LoginPage },
  { path: "/verify/", component: VerifyPage },
];

const f7params = {
  name: "STACOS",
  theme: "auto",
  routes,
  // Framework7's own dark mode follows the OS, matching the web client's default.
  darkMode: "auto",
};

export default function App() {
  const [ready, setReady] = useState(false);
  const [signedIn, setSignedIn] = useState(false);

  useEffect(() => {
    setSignOutHandler(() => setSignedIn(false));

    (async () => {
      const token = await loadTokens();
      if (token) {
        // A stored token is not proof of a live session: the security stamp may
        // have been rotated while the app was closed. Ask the server.
        try {
          await api.me();
          setSignedIn(true);
        } catch {
          setSignedIn(false);
        }
      }
      setReady(true);
    })();
  }, []);

  if (!ready) return null;

  return (
    <F7App {...f7params}>
      <View main url={signedIn ? "/" : "/login/"} />
    </F7App>
  );
}
