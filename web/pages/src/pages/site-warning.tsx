import { pageConfig } from "../lib/runtime";

/** The page is not on the host the server says it lives on: say so before anyone connects a wallet here. */
export function SiteWarning() {
  const { site } = pageConfig();
  if (!site || location.host === site) return null;
  return (
    <p className="mx-auto mb-4 w-full max-w-[880px] rounded-xl border border-[var(--cp-danger-fg)] px-4 py-3 text-[13px] leading-5 text-[var(--cp-danger-fg)]" role="alert">
      This page is on {location.host}, not {site}. Do not connect your wallet unless you are sure this is the Vanna page.
    </p>
  );
}
