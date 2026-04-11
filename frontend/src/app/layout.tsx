import "./globals.css";
import Providers from "@/components/Providers";
import Sidebar from "@/components/layout/Sidebar";
import BrokerStatusBar from "@/components/layout/BrokerStatusBar";
import RealTimeTicker from "@/components/layout/RealTimeTicker";
import LiveModeWarning from "@/components/layout/LiveModeWarning";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="bg-bg-primary text-text-primary min-h-screen">
        <Providers>
          <div className="flex flex-col h-screen overflow-hidden">
            <RealTimeTicker />
            <BrokerStatusBar />
            <LiveModeWarning />
            <div className="flex flex-1 overflow-hidden">
              <Sidebar />
              <main className="min-w-0 flex-1 overflow-y-auto px-4 py-4 lg:px-5">
                {children}
              </main>
            </div>
          </div>
        </Providers>
      </body>
    </html>
  );
}
