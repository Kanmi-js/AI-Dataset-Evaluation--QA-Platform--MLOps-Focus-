import { useCallback, useRef, useState } from "react";

interface Props {
  onUpload: (files: File[]) => Promise<void> | void;
  uploading?: boolean;
}

export function UploadDropzone({ onUpload, uploading }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const handleFiles = useCallback(
    (files: FileList | null) => {
      if (files && files.length > 0) void onUpload(Array.from(files));
    },
    [onUpload],
  );

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        handleFiles(e.dataTransfer.files);
      }}
      onClick={() => inputRef.current?.click()}
      className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-8 text-center transition-colors ${
        dragging
          ? "border-brand-500 bg-brand-50"
          : "border-slate-300 bg-white hover:border-brand-400"
      }`}
    >
      <input
        ref={inputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(e) => handleFiles(e.target.files)}
      />
      <p className="text-sm font-medium text-slate-700">
        {uploading ? "Uploading…" : "Drop images or text files here"}
      </p>
      <p className="mt-1 text-xs text-slate-400">
        or click to browse · pre-processing runs automatically
      </p>
    </div>
  );
}
