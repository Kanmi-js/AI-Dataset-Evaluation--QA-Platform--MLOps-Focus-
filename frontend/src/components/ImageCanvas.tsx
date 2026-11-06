import React, { useState, useRef } from 'react';

export interface BoundingBox {
  id: string;
  label: string;
  x: number;
  y: number;
  width: number;
  height: number;
  /**
   * Model confidence. Only present on boxes a detector produced — boxes drawn
   * by a reviewer have none, and the UI must not invent one for them.
   */
  confidence?: number;
  role?: 'Attacker' | 'Defender' | 'Midfield' | 'Goalkeeper' | 'Ball';
}

interface ImageCanvasProps {
  imageUrl: string;
  /** Previously saved annotations. Remount (via `key`) to load a new item. */
  initialBoxes?: BoundingBox[];
  onSaveBoxes?: (boxes: BoundingBox[]) => void;
}

export default function ImageCanvas({
  imageUrl,
  initialBoxes = [],
  onSaveBoxes,
}: ImageCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [boxes, setBoxes] = useState<BoundingBox[]>(initialBoxes);

  const [selectedBoxId, setSelectedBoxId] = useState<string | null>(
    initialBoxes[0]?.id ?? null,
  );
  const [isDrawing, setIsDrawing] = useState(false);
  const [startPos, setStartPos] = useState<{ x: number; y: number } | null>(null);
  const [activeRole, setActiveRole] = useState<'Attacker' | 'Defender' | 'Midfield' | 'Ball'>('Attacker');

  const handleMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    setIsDrawing(true);
    setStartPos({ x, y });
  };

  const handleMouseUp = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!isDrawing || !startPos || !containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    const currentX = e.clientX - rect.left;
    const currentY = e.clientY - rect.top;

    const width = Math.abs(currentX - startPos.x);
    const height = Math.abs(currentY - startPos.y);

    if (width > 20 && height > 20) {
      const newBox: BoundingBox = {
        id: `box-${Date.now()}`,
        label: `${activeRole} Target`,
        x: Math.min(startPos.x, currentX),
        y: Math.min(startPos.y, currentY),
        width,
        height,
        // No `confidence`: a reviewer drew this, so there is no model score.
        role: activeRole,
      };

      const updated = [...boxes, newBox];
      setBoxes(updated);
      setSelectedBoxId(newBox.id);
      if (onSaveBoxes) onSaveBoxes(updated);
    }

    setIsDrawing(false);
    setStartPos(null);
  };

  const deleteSelectedBox = () => {
    if (!selectedBoxId) return;
    const updated = boxes.filter((b) => b.id !== selectedBoxId);
    setBoxes(updated);
    setSelectedBoxId(null);
    if (onSaveBoxes) onSaveBoxes(updated);
  };

  return (
    <div className="flex flex-col gap-4 w-full">
      {/* Tactical Canvas Controls */}
      <div className="flex flex-wrap items-center justify-between gap-3 bg-pitch-card border border-pitch-border px-4 py-2.5 rounded-xl font-mono text-xs">
        <div className="flex items-center gap-2">
          <span className="text-zinc-500 uppercase">Detection Mode:</span>
          {(['Attacker', 'Defender', 'Midfield', 'Ball'] as const).map((role) => (
            <button
              key={role}
              onClick={() => setActiveRole(role)}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeRole === role
                  ? 'bg-volt text-pitch-dark font-bold'
                  : 'bg-pitch-dark text-zinc-400 hover:text-white border border-pitch-border'
              }`}
            >
              {role}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={deleteSelectedBox}
            disabled={!selectedBoxId}
            className="px-3 py-1 rounded bg-vector-crimson/10 border border-vector-crimson/30 text-vector-crimson hover:bg-vector-crimson hover:text-white transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
          >
            Remove Target
          </button>
          <span className="text-zinc-600">|</span>
          <span className="text-volt">
            {boxes.length} {boxes.length === 1 ? 'Target' : 'Targets'} Tracked
          </span>
        </div>
      </div>

      {/* Frame Inspection Arena */}
      <div
        ref={containerRef}
        onMouseDown={handleMouseDown}
        onMouseUp={handleMouseUp}
        className="relative bg-pitch-dark border border-pitch-border rounded-xl overflow-hidden cursor-crosshair select-none flex items-center justify-center p-2 min-h-[500px]"
      >
        <img
          src={imageUrl}
          alt="Tactical Feed"
          className="max-h-[640px] w-auto pointer-events-none rounded border border-pitch-border/40"
        />

        {/* Drawn Bounding Overlays */}
        {boxes.map((box) => {
          const isSelected = selectedBoxId === box.id;
          const isBall = box.role === 'Ball';

          return (
            <div
              key={box.id}
              onClick={(e) => {
                e.stopPropagation();
                setSelectedBoxId(box.id);
              }}
              style={{
                left: `${box.x}px`,
                top: `${box.y}px`,
                width: `${box.width}px`,
                height: `${box.height}px`,
              }}
              className={`absolute cursor-pointer border transition-all ${
                isSelected
                  ? isBall
                    ? 'border-vector-cyan bg-vector-cyan/15 shadow-cyan-glow'
                    : 'border-volt bg-volt/15 shadow-volt-glow'
                  : 'border-zinc-500/60 bg-black/20 hover:border-volt/80'
              }`}
            >
              {/* Telemetry Tag Above Box */}
              <div
                className={`absolute -top-6 left-0 font-mono text-[10px] px-1.5 py-0.5 rounded whitespace-nowrap flex items-center gap-1.5 ${
                  isSelected
                    ? isBall
                      ? 'bg-vector-cyan text-pitch-dark font-bold'
                      : 'bg-volt text-pitch-dark font-bold'
                    : 'bg-pitch-card border border-pitch-border text-zinc-300'
                }`}
              >
                <span>{box.label}</span>
                {box.confidence !== undefined ? (
                  <span>{(box.confidence * 100).toFixed(0)}%</span>
                ) : null}
              </div>

              {/* Tactical Corner Reticles */}
              <div className="absolute top-0 left-0 w-1.5 h-1.5 border-t-2 border-l-2 border-white" />
              <div className="absolute top-0 right-0 w-1.5 h-1.5 border-t-2 border-r-2 border-white" />
              <div className="absolute bottom-0 left-0 w-1.5 h-1.5 border-b-2 border-l-2 border-white" />
              <div className="absolute bottom-0 right-0 w-1.5 h-1.5 border-b-2 border-r-2 border-white" />
            </div>
          );
        })}
      </div>

      {/* Real-Time Coordinate Telemetry Readout */}
      {selectedBoxId && (
        <div className="p-3.5 rounded-xl border border-pitch-border bg-pitch-card flex items-center justify-between font-mono text-xs text-zinc-400">
          <div className="flex items-center gap-4">
            <span className="text-zinc-500 uppercase">Selected Entity:</span>
            <span className="text-white font-bold">
              {boxes.find((b) => b.id === selectedBoxId)?.label}
            </span>
          </div>
          <div className="flex items-center gap-6">
            <span>
              POS: <strong className="text-volt">{boxes.find((b) => b.id === selectedBoxId)?.x}</strong>,{' '}
              <strong className="text-volt">{boxes.find((b) => b.id === selectedBoxId)?.y}</strong>
            </span>
            <span>
              DIM: <strong className="text-white">{boxes.find((b) => b.id === selectedBoxId)?.width}</strong>x
              <strong className="text-white">{boxes.find((b) => b.id === selectedBoxId)?.height}</strong>
            </span>
            <span>
              CONFIDENCE:{' '}
              <strong className="text-vector-cyan">
                {(() => {
                  const c = boxes.find((b) => b.id === selectedBoxId)?.confidence;
                  // Manual boxes have no score; "—" beats printing a fake 0.0%.
                  return c === undefined ? '— (manual)' : `${(c * 100).toFixed(1)}%`;
                })()}
              </strong>
            </span>
          </div>
        </div>
      )}
    </div>
  );
}