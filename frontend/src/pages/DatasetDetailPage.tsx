import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  AlertTriangle,
  ArrowLeft,
  Crosshair,
  RefreshCw,
  Save,
  UploadCloud,
} from 'lucide-react';
import { api } from '../api/client';
import type { DataItem, DataItemDetail, DatasetWithItems, ItemStatus } from '../api/types';
import ImageCanvas, { type BoundingBox } from '../components/ImageCanvas';
import { TacticalRadar } from '../components/TacticalRadar';
import { BreakdownPanel } from '../components/BreakdownPanel';

const STATUS_STYLES: Record<ItemStatus, string> = {
  pending: 'text-zinc-400 border-pitch-border bg-pitch-dark',
  processing: 'text-vector-cyan border-vector-cyan/30 bg-vector-cyan/10',
  processed: 'text-volt border-volt/30 bg-volt/10',
  reviewed: 'text-volt border-volt/40 bg-volt/15',
  failed: 'text-vector-crimson border-vector-crimson/30 bg-vector-crimson/10',
};

const IN_FLIGHT: ItemStatus[] = ['pending', 'processing'];
const BREAKDOWN_IN_FLIGHT = ['queued', 'running'];

const breakdownStatus = (item: DataItem | undefined) => item?.item_metadata?.breakdown?.status;
const isBusy = (item: DataItem) =>
  IN_FLIGHT.includes(item.status) || BREAKDOWN_IN_FLIGHT.includes(breakdownStatus(item) ?? '');

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

export default function DatasetDetailPage() {
  const { id } = useParams<{ id: string }>();
  const datasetId = Number(id);
  const validId = Number.isInteger(datasetId) && datasetId > 0;

  const [dataset, setDataset] = useState<DatasetWithItems | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<DataItemDetail | null>(null);
  const [boxes, setBoxes] = useState<BoundingBox[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadDataset = useCallback(
    async (opts: { quiet?: boolean } = {}) => {
      if (!validId) return;
      try {
        if (!opts.quiet) setLoading(true);
        const res = await api.getDataset(datasetId);
        setDataset(res);
        setSelectedId((current) => {
          if (current && res.items.some((i) => i.id === current)) return current;
          return res.items[0]?.id ?? null;
        });
        setError(null);
      } catch (err) {
        setError(errorMessage(err, 'Could not load this dataset'));
      } finally {
        if (!opts.quiet) setLoading(false);
      }
    },
    [datasetId, validId],
  );

  useEffect(() => {
    void loadDataset();
  }, [loadDataset]);

  // Pre-processing and breakdown rendering happen in Celery workers, so state
  // changes after the response that started them. Poll only while something
  // is in flight.
  const inFlight = dataset?.items.some(isBusy) ?? false;
  useEffect(() => {
    if (!inFlight) return;
    const timer = setInterval(() => void loadDataset({ quiet: true }), 3000);
    return () => clearInterval(timer);
  }, [inFlight, loadDataset]);

  // The list payload omits presigned URLs; the detail endpoint mints them.
  // Re-fetch when the item's own status changes too, so a freshly processed
  // item picks up its poster frame and telemetry without a manual reload.
  const selectedItem = dataset?.items.find((i) => i.id === selectedId);
  const selectedStatus = selectedItem?.status;
  // Breakdown progress lives in item_metadata, so it has to drive a re-fetch
  // too, or the panel would never see the presigned video URL appear.
  const selectedBreakdown = breakdownStatus(selectedItem);
  const selectedStage = selectedItem?.item_metadata?.breakdown?.stage;
  useEffect(() => {
    if (selectedId === null) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const item = await api.getItem(selectedId);
        if (cancelled) return;
        setDetail(item);
        const saved = item.annotations?.boxes;
        setBoxes(Array.isArray(saved) ? (saved as BoundingBox[]) : []);
      } catch (err) {
        if (!cancelled) setError(errorMessage(err, 'Could not load that item'));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId, selectedStatus, selectedBreakdown, selectedStage]);

  const handleUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (!files || files.length === 0) return;
    try {
      setUploading(true);
      setError(null);
      const created = await api.uploadItems(datasetId, files);
      await loadDataset({ quiet: true });
      if (created[0]) setSelectedId(created[0].id);
    } catch (err) {
      setError(errorMessage(err, 'Upload failed'));
    } finally {
      setUploading(false);
      // Allow re-selecting the same file after a failure.
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleSaveAnnotations = async () => {
    if (!detail) return;
    try {
      setSaving(true);
      setError(null);
      await api.saveAnnotations(detail.id, { boxes });
      await loadDataset({ quiet: true });
      setDetail(await api.getItem(detail.id));
    } catch (err) {
      setError(errorMessage(err, 'Could not save annotations'));
    } finally {
      setSaving(false);
    }
  };

  const handleReprocess = async () => {
    if (!detail) return;
    try {
      setError(null);
      await api.reprocessItem(detail.id);
      await loadDataset({ quiet: true });
    } catch (err) {
      setError(errorMessage(err, 'Could not requeue this item'));
    }
  };

  const handleRegenerateBreakdown = async () => {
    if (!detail) return;
    try {
      setRegenerating(true);
      setError(null);
      await api.regenerateBreakdown(detail.id);
      await loadDataset({ quiet: true });
    } catch (err) {
      setError(errorMessage(err, 'Could not start a new breakdown'));
    } finally {
      setRegenerating(false);
    }
  };

  if (!validId) {
    return (
      <div className="flex h-screen w-full flex-col items-center justify-center gap-3 bg-pitch-dark text-zinc-400">
        <AlertTriangle className="h-8 w-8 text-vector-crimson" />
        <p className="font-mono text-sm">“{id}” is not a valid dataset id.</p>
        <Link to="/" className="font-mono text-xs text-volt hover:underline">
          Back to dashboard
        </Link>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex h-screen w-full items-center justify-center bg-pitch-dark text-zinc-400">
        <div className="flex items-center space-x-2">
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-volt border-t-transparent" />
          <span className="font-mono text-sm">Loading dataset workspace...</span>
        </div>
      </div>
    );
  }

  const items: DataItem[] = dataset?.items ?? [];
  const isVideo = detail !== null && detail.media_type === 'video';
  const telemetry = isVideo ? detail.item_metadata?.telemetry : undefined;
  // A video's raw file is an .mp4, which <img> cannot show — only its
  // processed poster frame can stand in. Images fall back to the raw upload.
  const canvasUrl = isVideo
    ? (detail?.processed_url ?? '')
    : (detail?.processed_url ?? detail?.raw_url ?? '');

  return (
    <div className="flex h-screen w-full flex-col overflow-hidden bg-pitch-dark font-sans text-zinc-100">
      {/* Top bar */}
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-pitch-border bg-pitch-card/60 px-6 backdrop-blur">
        <div className="flex items-center space-x-4">
          <Link
            to="/"
            className="flex items-center gap-1.5 text-xs text-zinc-400 transition-colors hover:text-zinc-100"
          >
            <ArrowLeft className="h-4 w-4" />
            <span>Dashboard</span>
          </Link>
          <div className="h-4 w-[1px] bg-pitch-border" />
          <h1 className="text-sm font-semibold tracking-wide text-white">
            {dataset?.name ?? `Dataset #${datasetId}`}
          </h1>
          <span className="rounded border border-pitch-border bg-pitch-dark px-2 py-0.5 font-mono text-[10px] uppercase text-zinc-400">
            {items.length} {items.length === 1 ? 'item' : 'items'}
          </span>
        </div>

        <div className="flex items-center gap-3">
          {detail ? (
            <button
              type="button"
              onClick={handleReprocess}
              className="flex cursor-pointer items-center gap-1.5 rounded border border-pitch-border bg-pitch-dark px-3 py-1.5 font-mono text-xs text-zinc-300 transition-colors hover:text-white"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              <span>Reprocess</span>
            </button>
          ) : null}
          <button
            type="button"
            onClick={handleSaveAnnotations}
            disabled={saving || !detail}
            className="flex cursor-pointer items-center gap-1.5 rounded bg-volt px-3 py-1.5 text-xs font-semibold text-pitch-dark transition-all hover:bg-volt-dim disabled:opacity-50"
          >
            <Save className="h-3.5 w-3.5" />
            <span>{saving ? 'Saving...' : 'Save Annotations'}</span>
          </button>
        </div>
      </header>

      {error ? (
        <div className="flex shrink-0 items-center gap-2 border-b border-vector-crimson/30 bg-vector-crimson/10 px-6 py-2 font-mono text-xs text-vector-crimson">
          <AlertTriangle className="h-3.5 w-3.5" />
          <span>{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            className="ml-auto cursor-pointer text-zinc-400 hover:text-white"
          >
            dismiss
          </button>
        </div>
      ) : null}

      <div className="flex flex-1 overflow-hidden">
        {/* Item list */}
        <aside className="flex w-80 shrink-0 flex-col overflow-y-auto border-r border-pitch-border bg-pitch-card/30">
          <div className="flex items-center justify-between border-b border-pitch-border p-3.5 text-xs">
            <span className="font-semibold uppercase tracking-wider text-zinc-300">
              Items
            </span>
            <input
              type="file"
              multiple
              ref={fileInputRef}
              onChange={handleUpload}
              accept="image/*,video/*,text/plain"
              className="hidden"
            />
            <button
              type="button"
              disabled={uploading}
              onClick={() => fileInputRef.current?.click()}
              className="flex cursor-pointer items-center gap-1 text-[11px] text-volt hover:underline disabled:opacity-50"
            >
              <UploadCloud className="h-3 w-3" />
              <span>{uploading ? 'Uploading...' : 'Upload'}</span>
            </button>
          </div>

          {items.length === 0 ? (
            <p className="p-4 font-mono text-[11px] leading-relaxed text-zinc-500">
              No items yet. Upload match stills or an .mp4 clip — clips are
              tracked, then turned into a voiced tactical breakdown.
            </p>
          ) : (
            <div className="divide-y divide-pitch-border/50">
              {items.map((item) => {
                const isSelected = selectedId === item.id;
                const meta = item.item_metadata ?? {};
                const dims =
                  meta.width && meta.height ? `${meta.width}×${meta.height}` : '—';
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setSelectedId(item.id)}
                    className={`flex w-full flex-col gap-1 p-3 text-left transition-colors ${
                      isSelected
                        ? 'border-l-2 border-volt bg-volt/10'
                        : 'hover:bg-pitch-hover'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-xs font-medium text-zinc-200">
                        {item.filename}
                      </span>
                      <span
                        className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase ${
                          STATUS_STYLES[item.status]
                        }`}
                      >
                        {item.status}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 font-mono text-[10px] text-zinc-500">
                      <span>{item.media_type}</span>
                      <span>·</span>
                      <span>{dims}</span>
                      {item.media_type === 'video' && meta.duration_sec ? (
                        <>
                          <span>·</span>
                          <span>{meta.duration_sec.toFixed(1)}s</span>
                        </>
                      ) : null}
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </aside>

        {/* Workspace */}
        <main className="flex flex-1 flex-col items-center overflow-y-auto bg-pitch-dark p-6">
          {!detail ? (
            <div className="flex w-full max-w-5xl flex-col items-center justify-center rounded-xl border border-dashed border-pitch-border p-8 text-center">
              <Crosshair className="mb-2 h-10 w-10 text-zinc-700" />
              <p className="text-sm font-medium text-zinc-400">
                Select an item on the left to inspect it.
              </p>
            </div>
          ) : (
            <div className="flex w-full max-w-5xl flex-col items-center gap-8">
              {detail.error ? (
                <div className="w-full rounded-xl border border-vector-crimson/30 bg-vector-crimson/10 p-3 font-mono text-xs text-vector-crimson">
                  Pre-processing failed: {detail.error}
                </div>
              ) : null}

              {isVideo ? (
                <BreakdownPanel
                  breakdown={detail.item_metadata?.breakdown}
                  videoUrl={detail.breakdown_url}
                  analysis={detail.item_metadata?.analysis}
                  telemetry={telemetry}
                  itemInFlight={IN_FLIGHT.includes(detail.status)}
                  regenerating={regenerating}
                  onRegenerate={handleRegenerateBreakdown}
                />
              ) : null}

              <div className="w-full rounded-xl border border-pitch-border bg-pitch-card/40 p-2 shadow-2xl backdrop-blur">
                {canvasUrl ? (
                  <ImageCanvas
                    key={detail.id}
                    imageUrl={canvasUrl}
                    initialBoxes={boxes}
                    onSaveBoxes={setBoxes}
                  />
                ) : (
                  <p className="p-8 text-center font-mono text-xs text-zinc-500">
                    Waiting for the worker to produce a preview for this item.
                  </p>
                )}
              </div>

              {isVideo ? (
                telemetry ? (
                  <div className="w-full">
                    <TacticalRadar telemetry={telemetry} filename={detail.filename} />
                  </div>
                ) : (
                  <div className="w-full rounded-xl border border-dashed border-pitch-border p-6 text-center font-mono text-xs text-zinc-500">
                    {IN_FLIGHT.includes(detail.status)
                      ? 'Telemetry is still being generated for this clip...'
                      : 'No telemetry on this clip. Try Reprocess.'}
                  </div>
                )
              ) : null}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
