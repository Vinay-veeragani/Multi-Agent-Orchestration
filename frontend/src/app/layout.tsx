import type { Metadata } from "next";
import { Geist, JetBrains_Mono } from "next/font/google";
import { AppShell } from "@/components/layout/app-shell";
import { Providers } from "@/components/providers";
import { getHealth } from "@/lib/api";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const jetbrainsMono = JetBrains_Mono({
  variable: "--font-jetbrains-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "Agent Orchestration Engine",
    template: "%s · Agent Orchestration Engine",
  },
  description: "Live view of agent executions, workflows, and agents.",
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  // Best-effort: if the API is unreachable, the top bar just shows
  // "unreachable" rather than taking the whole shell down over a health
  // check -- each page's own error.tsx still covers a genuinely broken
  // data fetch for that page.
  const health = await getHealth().catch(() => null);

  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${jetbrainsMono.variable} h-full antialiased`}
    >
      <body className="h-full bg-background text-foreground">
        <Providers>
          <AppShell health={health}>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
