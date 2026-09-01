"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/agents", label: "Agents" },
  { href: "/benchmarks", label: "Benchmarks" },
  { href: "/workflows", label: "Workflows" },
];

export function NavLinks() {
  const pathname = usePathname();

  return (
    <>
      {LINKS.map((link) => {
        const active = pathname === link.href || pathname.startsWith(`${link.href}/`);
        return (
          <Link
            key={link.href}
            href={link.href}
            aria-current={active ? "page" : undefined}
            className={`text-sm hover:underline ${
              active
                ? "font-medium text-neutral-900 dark:text-neutral-100"
                : "text-neutral-500"
            }`}
          >
            {link.label}
          </Link>
        );
      })}
    </>
  );
}
