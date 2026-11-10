import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Floorwalker · agentic ad-monetization control plane",
  description:
    "An AI agent that monitors a simulated mobile-game ad stack, finds the root cause of revenue incidents and ships guarded fixes. Break something and watch.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
