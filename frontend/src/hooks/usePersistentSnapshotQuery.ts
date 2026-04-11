"use client";

import { useEffect, useState } from "react";
import {
  useQuery,
  type QueryKey,
  type UseQueryOptions,
  type UseQueryResult,
} from "@tanstack/react-query";

type SnapshotEnvelope<TData> = {
  savedAt: string;
  data: TData;
};

type PersistentSnapshotQueryOptions<TData, TError = Error> = Omit<
  UseQueryOptions<TData, TError, TData, QueryKey>,
  "queryKey" | "queryFn" | "initialData" | "initialDataUpdatedAt"
> & {
  queryKey: QueryKey;
  queryFn: () => Promise<TData>;
  storageKey: string;
};

export type PersistentSnapshotQueryResult<TData, TError = Error> = Omit<
  UseQueryResult<TData, TError>,
  "data"
> & {
  data: TData | undefined;
  snapshotSavedAt: string | null;
  hasSnapshot: boolean;
  isShowingSnapshot: boolean;
};

function loadSnapshot<TData>(storageKey: string): SnapshotEnvelope<TData> | null {
  if (typeof window === "undefined") return null;

  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as SnapshotEnvelope<TData>;
    if (!parsed?.savedAt) return null;

    // Invalidate snapshots older than 4 hours to prevent showing stale data
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

function persistSnapshot<TData>(storageKey: string, envelope: SnapshotEnvelope<TData>) {
  if (typeof window === "undefined") return;

  try {
    window.localStorage.setItem(storageKey, JSON.stringify(envelope));
  } catch {
    // Ignore storage write failures and continue with in-memory state.
  }
}

export function usePersistentSnapshotQuery<TData, TError = Error>({
  storageKey,
  queryKey,
  queryFn,
  ...options
}: PersistentSnapshotQueryOptions<TData, TError>): PersistentSnapshotQueryResult<TData, TError> {
  const [snapshot, setSnapshot] = useState<SnapshotEnvelope<TData> | null>(() => loadSnapshot<TData>(storageKey));

  const query = useQuery<TData, TError, TData, QueryKey>({
    ...options,
    queryKey,
    queryFn,
    initialData: snapshot?.data,
    initialDataUpdatedAt: snapshot ? new Date(snapshot.savedAt).getTime() : undefined,
  });

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

  const effectiveData = query.data ?? snapshot?.data;
  const isShowingSnapshot = Boolean(snapshot?.data) && query.isError;

  return {
    ...query,
    data: effectiveData,
    snapshotSavedAt: snapshot?.savedAt ?? null,
    hasSnapshot: Boolean(snapshot?.data),
    isShowingSnapshot,
  };
}
