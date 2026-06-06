import "./globals.css";
import type { Metadata } from "next";

import Providers from "@/components/Providers";
import { THEME_NO_FLASH_SCRIPT } from "@/components/ThemeProvider";
import Sidebar from "@/components/layout/Sidebar";
import TopBar from "@/components/layout/TopBar";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export const metadata: Metadata = {
  title: "Nomad Curie · v2",
  description: "Reorganised trader workspace — v2 preview.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_NO_FLASH_SCRIPT }} />
      </head>
      <body className="bg-bg-primary text-text-primary min-h-screen">
        <Providers>
          <div className="flex flex-col h-screen overflow-hidden">
            <TopBar />
            <div className="flex flex-1 overflow-hidden">
              <Sidebar />
              <main className="relative z-0 min-w-0 flex-1 overflow-y-auto px-3 py-3 lg:px-4">
                {children}
              </main>
            </div>
          </div>
        </Providers>
      </body>
    </html>
  );
}
