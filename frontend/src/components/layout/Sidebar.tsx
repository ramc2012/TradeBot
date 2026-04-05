"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard, TrendingUp, BarChart3, Globe, Bot, Settings, FlaskConical, Database, Activity, Boxes,
} from "lucide-react";
import { clsx } from "clsx";

const NAV = [
  { href: "/", label: "Home", icon: LayoutDashboard },
  { href: "/trading", label: "Trading", icon: TrendingUp },
  { href: "/commodity", label: "Commodity", icon: Boxes },
  { href: "/analytics", label: "Analytics", icon: BarChart3 },
  { href: "/market", label: "Market", icon: Globe },
  { href: "/agent", label: "Agent", icon: Bot },
  { href: "/backtester", label: "Backtester", icon: FlaskConical },
  { href: "/data", label: "F&O Data", icon: Database },
  { href: "/analysis", label: "MACD Analysis", icon: Activity },
  { href: "/settings", label: "Settings", icon: Settings },
];

export default function Sidebar() {
  const pathname = usePathname();
  return (
    <nav className="w-14 bg-bg-secondary border-r border-bg-border flex flex-col items-center py-4 gap-1 shrink-0">
      {/* Logo */}
      <div className="mb-4">
        <span className="text-accent-green font-mono font-bold text-xs">NC</span>
      </div>
      {NAV.map(({ href, label, icon: Icon }) => {
        const active = pathname === href || (href !== "/" && pathname.startsWith(href));
        return (
          <Link
            key={href}
            href={href}
            title={label}
            className={clsx(
              "w-10 h-10 flex items-center justify-center rounded-lg transition-colors",
              active
                ? "bg-accent-blue/20 text-accent-blue"
                : "text-text-muted hover:bg-bg-hover hover:text-text-primary"
            )}
          >
            <Icon size={18} />
          </Link>
        );
      })}
    </nav>
  );
}
