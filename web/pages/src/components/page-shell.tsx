import type { ReactNode } from "react";

/** Copied from the Stellar MCP pages (sign-service/web/components/page-shell.tsx); the network chip names Solana. */
const vannaInk = { src: "/static/vanna-wordmark.png" };
const vannaWhite = { src: "/static/vanna-wordmark-white.png" };

/** Vanna's wordmark: the dark ink on light pages, the white one on dark pages. */
export function VannaLogo({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex ${className}`}>
      {/* eslint-disable-next-line @next/next/no-img-element -- static export: imported so the URL carries the asset prefix */}
      <img src={vannaInk.src} alt="Vanna" width={77} height={24} className="h-7 w-auto dark:hidden" />
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={vannaWhite.src} alt="Vanna" width={77} height={24} className="hidden h-7 w-auto dark:inline" />
    </span>
  );
}

/** The wordmark inside a card, sized by the caller (`className` sets the height). */
export function VannaMark({ className = "h-[18px]" }: { className?: string }) {
  return (
    <span className="inline-flex">
      {/* eslint-disable-next-line @next/next/no-img-element -- static export: imported so the URL carries the asset prefix */}
      <img src={vannaInk.src} alt="Vanna" width={77} height={24} className={`${className} w-auto dark:hidden`} />
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={vannaWhite.src} alt="Vanna" width={77} height={24} className={`hidden ${className} w-auto dark:inline`} />
    </span>
  );
}

/** The network a page acts on, always visible: testnet and mainnet must never be confused. */
export function NetworkChip({ network }: { network: string }) {
  const mainnet = network === "mainnet";
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border border-vgray-100 bg-surface px-2.5 py-1 text-[12px] font-semibold text-vgray-500">
      <span className={`h-1.5 w-1.5 rounded-full ${mainnet ? "bg-[var(--cp-emerald)]" : "bg-[var(--cp-amber)]"}`} aria-hidden="true" />
      {mainnet ? "Solana mainnet" : "Solana test network"}
    </span>
  );
}

/** A navbar (wordmark far left, network far right) over one centred column: the frame both pages share. */
export function PageShell({ network = "testnet", aside, wide = false, review = false, medium = false, children }: {
  network?: string; aside?: ReactNode; wide?: boolean; review?: boolean; medium?: boolean; children: ReactNode;
}) {
  const width = review ? "max-w-[1080px]" : wide ? "max-w-[880px]" : medium ? "max-w-[640px]" : "max-w-[460px]";
  return (
    <>
      <header className="sticky top-0 z-40 flex h-16 w-full items-center justify-between gap-3 border-b border-[var(--line)] bg-[var(--bg)] px-4 sm:px-8">
        <VannaLogo />
        <div className="flex items-center gap-2">
          {aside}
          <NetworkChip network={network} />
        </div>
      </header>
      <main className="flex flex-1 flex-col items-center px-4 pb-28 pt-6 sm:pt-12">
        <div className={`flex w-full ${width} flex-col items-stretch`}>{children}</div>
      </main>
    </>
  );
}
