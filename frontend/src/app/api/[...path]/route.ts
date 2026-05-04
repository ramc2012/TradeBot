import { type NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const DEFAULT_BACKEND_PORT = process.env.NEXT_PUBLIC_BACKEND_PORT || "8000";

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function deriveBackendBaseUrl(request: NextRequest): string {
  const configured = process.env.NEXT_PUBLIC_API_URL || process.env.API_URL;
  if (configured) {
    return trimTrailingSlash(configured);
  }

  const host = request.headers.get("x-forwarded-host") || request.headers.get("host") || "";
  const protocol = request.headers.get("x-forwarded-proto") || "https";
  if (host.includes("-ui-") && (host.endsWith(".run.app") || host.endsWith(".a.run.app"))) {
    return `${protocol}://${host.replace("-ui-", "-api-")}`;
  }

  const hostname = host.split(":")[0] || "localhost";
  if (hostname === "localhost" || hostname === "127.0.0.1" || hostname === "0.0.0.0") {
    return `http://${hostname}:${DEFAULT_BACKEND_PORT}`;
  }

  return `${protocol}://${host}`;
}

function buildHeaders(request: NextRequest): Headers {
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("content-length");
  headers.delete("connection");
  headers.delete("accept-encoding");
  headers.set("x-forwarded-host", request.headers.get("host") || "");
  return headers;
}

async function proxy(request: NextRequest, { params }: { params: { path?: string[] } }) {
  const path = params.path?.join("/") || "";
  const target = new URL(`/api/${path}`, deriveBackendBaseUrl(request));
  target.search = request.nextUrl.search;

  const method = request.method.toUpperCase();
  const hasBody = method !== "GET" && method !== "HEAD";
  const upstream = await fetch(target, {
    method,
    headers: buildHeaders(request),
    body: hasBody ? await request.arrayBuffer() : undefined,
    redirect: "manual",
  });

  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.delete("content-encoding");
  responseHeaders.delete("content-length");
  responseHeaders.delete("transfer-encoding");

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const HEAD = proxy;
