import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "LLMGate Admin",
  description: "Operator dashboard for LLMGate",
};

const links = [
  ["/", "Overview"],
  ["/keys", "Keys"],
  ["/teams", "Teams"],
  ["/usage", "Usage"],
  ["/logs", "Logs"],
  ["/routing", "Routing"],
  ["/login", "Login"],
] as const;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link
          href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600;700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <nav>
          <div className="brand">LLMGate</div>
          {links.map(([href, label]) => (
            <Link key={href} href={href}>
              {label}
            </Link>
          ))}
        </nav>
        <main>{children}</main>
      </body>
    </html>
  );
}
