"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  useQuery,
  useQueryClient,
  type QueryKey,
  type UseQueryOptions,
  type UseQueryResult,
} from "@tanstack/react-query";

type SnapshotEnvelope<TData> = {
  savedAt: string;
  data: TData;
};

type LiveSocketHandle = {
  close: () => void;
};

type LiveSocketFactory<TData> = (
  onData: (data: TData) => void,
  onStatusChange?: (connected: boolean) => void,
) => LiveSocketHandle;

type LiveSnapshotQueryOptions<TData, TError = Error> = Omit<
  UseQueryOptions<TData, TError, TData, QueryKey>,
  "queryKey" | "queryFn" | "initialData" | "initialDataUpdatedAt"
> & {
  queryKey: QueryKey;
  queryFn: () => Promise<TData>;
  storageKey?: string;
  streamFactory?: LiveSocketFactory<TData>;
  streamWhenHidden?: boolean;
};

export type LiveSnapshotQueryResult<TData, TError = Error> = Omit<
  UseQueryResult<TData, TError>,
  "data"
> & {
  data: TData | undefined;
  snapshotSavedAt: string | null;
  hasSnapshot: boolean;
  isShowingSnapshot: boolean;
  isStreamConnected: boolean;
};

function loadSnapshot<TData>(storageKey?: string): SnapshotEnvelope<TData> | null {
  if (!storageKey || typeof window === "undefined") return null;

  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as SnapshotEnvelope<TData>;
    if (!parsed?.savedAt) return null;

    const savedTime = new Date(parsed.savedAt).getTime();
    const fourHoursMs = 4 * 60 * 60 * 1000;
    if (Date.now() - savedTime > fourHoursMs) {
      window.localStorage.removeItem(storageKey);
      return null;
    }

    return parsed;
  } catch {
    return null;
  }
}

function persistSnapshot<TData>(storageKey: string | undefined, envelope: SnapshotEnvelope<TData>) {
  if (!storageKey || typeof window === "undefined") return;

  try {
    window.localStorage.setItem(storageKey, JSON.stringify(envelope));
  } catch {
    // Keep working without persistence when localStorage is unavailable.
  }
}

export function useLiveSnapshotQuery<TData, TError = Error>({
  storageKey,
  streamFactory,
  streamWhenHidden = false,
  queryKey,
  queryFn,
  ...options
}: LiveSnapshotQueryOptions<TData, TError>): LiveSnapshotQueryResult<TData, TError> {
  const queryClient = useQueryClient();
  const queryKeyHash = useMemo(() => JSON.stringify(queryKey), [queryKey]);
  const streamFactoryRef = useRef(streamFactory);
  const [snapshot, setSnapshot] = useState<SnapshotEnvelope<TData> | null>(() => loadSnapshot<TData>(storageKey));
  const [isVisible, setIsVisible] = useState(() =>
    typeof document === "undefined" ? true : document.visibilityState === "visible",
  );
  const [isStreamConnected, setIsStreamConnected] = useState(false);

  useEffect(() => {
    streamFactoryRef.current = streamFactory;
  }, [streamFactory]);

  const query = useQuery<TData, TError, TData, QueryKey>({
    ...options,
    queryKey,
    queryFn,
    initialData: snapshot?.data,
    initialDataUpdatedAt: snapshot ? new Date(snapshot.savedAt).getTime() : undefined,
  });

  useEffect(() => {
    if (typeof document === "undefined") return undefined;

    const handleVisibilityChange = () => {
      setIsVisible(document.visibilityState === "visible");
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, []);

  useEffect(() => {
    if (query.isSuccess && query.data !== undefined) {
      const nextSnapshot = {
        data: query.data,
        savedAt: new Date().toISOString(),
      };
      setSnapshot(nextSnapshot);
      persistSnapshot(storageKey, nextSnapshot);
    }
  }, [query.data, query.dataUpdatedAt, query.isSuccess, storageKey]);

  useEffect(() => {
    const activeStreamFactory = streamFactoryRef.current;
    if (!activeStreamFactory || options.enabled === false) {
      setIsStreamConnected(false);
      return undefined;
    }

    if (!streamWhenHidden && !isVisible) {
      setIsStreamConnected(false);
      return undefined;
    }

    const socket = activeStreamFactory((nextData) => {
      queryClient.setQueryData(queryKey, nextData);
      if (storageKey) {
        const nextSnapshot = {
          data: nextData,
          savedAt: new Date().toISOString(),
        };
        setSnapshot(nextSnapshot);
        persistSnapshot(storageKey, nextSnapshot);
      }
    }, setIsStreamConnected);

    return () => {
      setIsStreamConnected(false);
      socket.close();
    };
  }, [isVisible, options.enabled, queryClient, queryKeyHash, storageKey, streamWhenHidden]);

  const effectiveData = query.data ?? snapshot?.data;
  const isShowingSnapshot = Boolean(snapshot?.data) && query.isError;

  return {
    ...query,
    data: effectiveData,
    snapshotSavedAt: snapshot?.savedAt ?? null,
    hasSnapshot: Boolean(snapshot?.data),
    isShowingSnapshot,
    isStreamConnected,
  };
}
