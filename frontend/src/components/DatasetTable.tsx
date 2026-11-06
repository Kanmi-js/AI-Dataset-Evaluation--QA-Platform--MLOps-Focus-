import type { DataItem } from "../api/types";
import { StatusBadge } from "./StatusBadge";

interface Props {
  items: DataItem[];
  selectedId: number | null;
  onSelect: (item: DataItem) => void;
}

function formatBytes(bytes: number | null): string {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
}

export function DatasetTable({ items, selectedId, onSelect }: Props) {
  if (items.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-slate-300 p-8 text-center text-sm text-slate-400">
        No items yet. Upload files to begin pre-processing.
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table className="min-w-full divide-y divide-slate-200 text-sm">
        <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-4 py-2">File</th>
            <th className="px-4 py-2">Type</th>
            <th className="px-4 py-2">Status</th>
            <th className="px-4 py-2">Quality</th>
            <th className="px-4 py-2">Size</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {items.map((item) => {
            const flag = (item.item_metadata?.quality_flag as string) ?? "—";
            const selected = item.id === selectedId;
            return (
              <tr
                key={item.id}
                onClick={() => onSelect(item)}
                className={`cursor-pointer transition-colors ${
                  selected ? "bg-brand-50" : "hover:bg-slate-50"
                }`}
              >
                <td className="max-w-xs truncate px-4 py-2 font-medium text-slate-800">
                  {item.filename}
                </td>
                <td className="px-4 py-2 capitalize text-slate-500">
                  {item.media_type}
                </td>
                <td className="px-4 py-2">
                  <StatusBadge status={item.status} />
                </td>
                <td className="px-4 py-2">
                  <span
                    className={`capitalize ${
                      flag !== "ok" && flag !== "—"
                        ? "text-rose-600"
                        : "text-slate-500"
                    }`}
                  >
                    {flag}
                  </span>
                </td>
                <td className="px-4 py-2 text-slate-500">
                  {formatBytes(item.size_bytes)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
