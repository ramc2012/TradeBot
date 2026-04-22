import "./globals.css";
import Providers from "@/components/Providers";
import AppErrorBoundary from "@/components/errors/AppErrorBoundary";
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
            <AppErrorBoundary
              compact
              scopeLabel="Shell Error"
              title="The live status strip failed."
              detail="Ticker or broker-status components crashed. The workspace can still run; retry this shell section or continue from the overview."
            >
              <>
                <RealTimeTicker />
                <BrokerStatusBar />
                <LiveModeWarning />
              </>
            </AppErrorBoundary>
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
                <main className="relative z-0 min-w-0 flex-1 overflow-y-auto px-3 py-3 lg:px-4">
                  {children}
                </main>
              </AppErrorBoundary>
            </div>
          </div>
        </Providers>
      </body>
    </html>
  );
}
