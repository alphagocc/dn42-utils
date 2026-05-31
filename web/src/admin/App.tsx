import { useState } from "react";
import { Login } from "./Login";
import { Dashboard } from "./Dashboard";

export function App() {
  const [token, setToken] = useState(
    () => sessionStorage.getItem("dn42ctl_admin_token") || "",
  );

  if (!token) {
    return <Login onLogin={setToken} />;
  }

  return (
    <Dashboard
      onLogout={() => {
        sessionStorage.removeItem("dn42ctl_admin_token");
        setToken("");
      }}
    />
  );
}
