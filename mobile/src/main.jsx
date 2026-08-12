import React from "react";
import { createRoot } from "react-dom/client";
import Framework7 from "framework7/lite-bundle";
import Framework7React from "framework7-react";

import "framework7/css/bundle";
import "./styles.css";

import App from "./App.jsx";

Framework7.use(Framework7React);

createRoot(document.getElementById("app")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
