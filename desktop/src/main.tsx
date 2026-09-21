import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { DataProvider } from "./data";
import { ThemeProvider } from "./theme";
import { I18nProvider } from "./i18n";
import { ConfirmProvider } from "./ui";
import "./styles.css";
import "./styles/shared.css";

if (/Mac/i.test(navigator.platform)) document.documentElement.classList.add("is-mac");

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nProvider>
      <ThemeProvider>
        <ConfirmProvider>
          <DataProvider>
            <App />
          </DataProvider>
        </ConfirmProvider>
      </ThemeProvider>
    </I18nProvider>
  </StrictMode>,
);
