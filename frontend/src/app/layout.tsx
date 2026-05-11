import "./globals.css";
import Providers from "@/components/Providers";
import AppErrorBoundary from "@/components/errors/AppErrorBoundary";
import AppTokenGate from "@/components/auth/AppTokenGate";
import Sidebar from "@/components/layout/Sidebar";
import BrokerStatusBar from "@/components/layout/BrokerStatusBar";
import RealTimeTicker from "@/components/layout/RealTimeTicker";
import LiveModeWarning from "@/components/layout/LiveModeWarning";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="bg-bg-primary text-text-primary min-h-screen">
        <Providers>
          <AppTokenGate>
            <div className="flex flex-col h-screen overflow-hidden">
              <>
                <AppErrorBoundary
                  compact
                  scopeLabel="Ticker Error"
                  title="The live ticker strip failed."
                  detail="The workspace can still run while the index ticker recovers."
                >
                  <RealTimeTicker />
                </AppErrorBoundary>
                <AppErrorBoundary
                  compact
                  scopeLabel="Broker Error"
                  title="The broker status strip failed."
                  detail="The workspace can still run while broker and portfolio status recover."
                >
                  <BrokerStatusBar />
                </AppErrorBoundary>
                <AppErrorBoundary
                  compact
                  scopeLabel="Mode Error"
                  title="The trading-mode warning failed."
                  detail="The workspace can still run while the mode warning recovers."
                >
                  <LiveModeWarning />
                </AppErrorBoundary>
              </>
              <div className="flex flex-1 overflow-hidden">
                <AppErrorBoundary
                  compact
                  scopeLabel="Navigation Error"
                  title="The sidebar failed."
                  detail="Navigation crashed while rendering the active workspace links. Retry the sidebar or return to the overview."
                >
                  <Sidebar />
                </AppErrorBoundary>
                <AppErrorBoundary
                  scopeLabel="Workspace Error"
                  title="The workspace content failed to render."
                  detail="A component crashed while rendering this workspace. Retry the section without restarting the whole app shell."
                >
                  <main className="relative z-0 min-w-0 flex-1 overflow-y-auto px-2 py-2 lg:px-3">
                    {children}
                  </main>
                </AppErrorBoundary>
              </div>
            </div>
          </AppTokenGate>
        </Providers>
      </body>
    </html>
  );
}
