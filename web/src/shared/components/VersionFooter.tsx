import { useEffect, useState } from "react";
import { api, API_PATHS } from "../api";

interface VersionInfo {
  version: string;
  commit: string | null;
}

export function VersionFooter() {
  const [info, setInfo] = useState<VersionInfo | null>(null);

  useEffect(() => {
    api<VersionInfo>(API_PATHS.version)
      .then(setInfo)
      .catch(() => {});
  }, []);

  if (!info) return null;

  const label = info.commit
    ? `v${info.version} (${info.commit.slice(0, 7)})`
    : `v${info.version}`;

  return (
    <span className="text-xs text-zinc-400 dark:text-zinc-600">{label}</span>
  );
}
