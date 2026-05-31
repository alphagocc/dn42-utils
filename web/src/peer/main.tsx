import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../shared/index.css";
import { ToastProvider } from "../shared/components/Toast";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ToastProvider>
      <App />
    </ToastProvider>
  </StrictMode>,
);
