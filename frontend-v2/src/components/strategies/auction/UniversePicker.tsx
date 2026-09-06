"use client";
import { useEffect, useId, useState } from "react";
export function UniversePicker({ value, symbols, onChange }: { value: string; symbols: string[]; onChange: (value: string) => void }) {
  const id = useId();
  const [search, setSearch] = useState(value);
  useEffect(() => setSearch(value), [value]);
  return <label className="flex items-center gap-2 text-xs text-text-muted"><span className="hidden sm:inline">{symbols.length} symbols</span><input aria-label="Search auction universe" list={id} value={search} placeholder="Search symbol" className="w-36 rounded-md border border-border-subtle bg-bg-primary px-3 py-2 text-xs text-text-primary outline-none focus:border-indigo-400" onBlur={() => setSearch(value)} onChange={(e) => { const next = e.target.value.toUpperCase(); setSearch(next); if (symbols.includes(next)) onChange(next); }} /><datalist id={id}>{symbols.map(s => <option key={s} value={s} />)}</datalist></label>;
}
