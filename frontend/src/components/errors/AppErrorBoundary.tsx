"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";
import { usePathname } from "next/navigation";

import RouteErrorState from "@/components/errors/RouteErrorState";
import SectionErrorFallback from "@/components/errors/SectionErrorFallback";

type AppErrorBoundaryProps = {
  children: ReactNode;
  title: string;
  detail: string;
  scopeLabel?: string;
  compact?: boolean;
  homeHref?: string;
  homeLabel?: string;
};

type AppErrorBoundaryInnerProps = AppErrorBoundaryProps & {
  resetKey: string;
};

type AppErrorBoundaryState = {
  hasError: boolean;
};

class AppErrorBoundaryInner extends Component<AppErrorBoundaryInnerProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = {
    hasError: false,
  };

  static getDerivedStateFromError(): AppErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[AppErrorBoundary]", error, info.componentStack);
  }

  componentDidUpdate(prevProps: AppErrorBoundaryInnerProps) {
    if (prevProps.resetKey !== this.props.resetKey && this.state.hasError) {
      this.setState({ hasError: false });
    }
  }

  private handleReset = () => {
    this.setState({ hasError: false });
  };

  render() {
    const {
      children,
      title,
      detail,
      scopeLabel,
      compact = false,
      homeHref,
      homeLabel,
    } = this.props;

    if (!this.state.hasError) {
      return children;
    }

    if (compact) {
      return (
        <SectionErrorFallback
          title={title}
          detail={detail}
          onRetry={this.handleReset}
          scopeLabel={scopeLabel}
          homeHref={homeHref}
          homeLabel={homeLabel}
        />
      );
    }

    return (
      <RouteErrorState
        title={title}
        detail={detail}
        reset={this.handleReset}
        scopeLabel={scopeLabel ?? "Component Error"}
        homeHref={homeHref}
        homeLabel={homeLabel}
        retryLabel="Retry Section"
      />
    );
  }
}

export default function AppErrorBoundary(props: AppErrorBoundaryProps) {
  const pathname = usePathname() ?? "";
  return <AppErrorBoundaryInner {...props} resetKey={pathname} />;
}
