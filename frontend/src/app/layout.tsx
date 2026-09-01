import type { Metadata } from "next";
import Link from "next/link";
import { Geist, Geist_Mono } from "next/font/google";
import { NavLinks } from "./nav-links";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "Agent Orchestration Engine",
    template: "%s · Agent Orchestration Engine",
  },
  description: "Live view of agent executions, workflows, and agents.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <header className="border-b border-black/10 dark:border-white/15">
          <div className="mx-auto flex max-w-4xl items-center gap-6 px-6 py-3">
            <Link href="/" className="font-medium">
              Agent Orchestration Engine
            </Link>
            <NavLinks />
          </div>
        </header>
        <main className="flex-1">{children}</main>
      </body>
    </html>
  );
}
