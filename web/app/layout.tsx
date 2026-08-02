import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Season Highlights",
  description: "Review GameChanger clips and build player highlight reels.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
