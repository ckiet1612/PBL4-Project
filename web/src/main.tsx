import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./styles/tokens.css";
import "./styles/layout.css";
import "./styles/components.css";

const root = document.getElementById("root");

if (root === null) {
  throw new Error("Nexa web root element is missing");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
