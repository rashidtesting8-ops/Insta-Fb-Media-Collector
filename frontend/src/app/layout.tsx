import type { Metadata } from "next";
import AppShell from "@/components/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Atelier Media · Pet Grooming Media Intelligence",
  description: "Curated media collection, AI visual direction, rights review, and private Google Drive delivery for pet grooming studios.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-[#FAF8F5] text-[#181614] antialiased">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
