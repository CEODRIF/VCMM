"use client";

import * as React from "react";
import { FileVideo, Upload, X } from "lucide-react";

import { cn, formatBytes } from "@/lib/utils";

interface UploadAreaProps {
  file: File | null;
  onFileSelected: (file: File) => void;
  onFileCleared: () => void;
  error?: string | null;
  /** Disable picking while an upload transfer is in progress. */
  disabled?: boolean;
}

const VIDEO_ACCEPT = "video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm";

/**
 * Drag-and-drop video file selection.
 *
 * Phase 3: the selected File is transferred directly from the browser to
 * private Supabase Storage (signed upload URL) — the form orchestrates that
 * flow; this component only handles selection, metadata display and errors.
 */
export function UploadArea({ file, onFileSelected, onFileCleared, error, disabled }: UploadAreaProps) {
  const [dragActive, setDragActive] = React.useState(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  function acceptFile(candidate: File | undefined | null) {
    if (!candidate) return;
    onFileSelected(candidate);
  }

  return (
    <div className="flex flex-col gap-2">
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-label="Upload a video file"
        aria-disabled={disabled || undefined}
        onClick={() => {
          if (!disabled) inputRef.current?.click();
        }}
        onKeyDown={(event) => {
          if (disabled) return;
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(event) => {
          if (disabled) return;
          event.preventDefault();
          setDragActive(true);
        }}
        onDragLeave={() => setDragActive(false)}
        onDrop={(event) => {
          if (disabled) return;
          event.preventDefault();
          setDragActive(false);
          acceptFile(event.dataTransfer.files?.[0]);
        }}
        className={cn(
          "flex flex-col items-center justify-center gap-3 rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors",
          disabled
            ? "cursor-not-allowed border-input opacity-70"
            : dragActive
              ? "cursor-pointer border-primary bg-primary/10"
              : "cursor-pointer border-input bg-background/40 hover:border-primary/50",
          error && "border-destructive"
        )}
      >
        <span className="flex h-11 w-11 items-center justify-center rounded-full bg-accent text-accent-foreground">
          {file ? <FileVideo size={20} /> : <Upload size={20} />}
        </span>
        {file ? (
          <div className="flex items-center gap-2 text-sm">
            <span className="max-w-[16rem] truncate font-medium">{file.name}</span>
            <span className="text-muted-foreground">({formatBytes(file.size)})</span>
            {!disabled && (
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  onFileCleared();
                }}
                className="rounded p-1 text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
                aria-label="Remove selected file"
              >
                <X size={14} />
              </button>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            <p className="text-sm font-medium">{disabled ? "Upload in progress…" : "Drag and drop a video file"}</p>
            {!disabled && (
              <p className="text-xs text-muted-foreground">or click to browse — MP4, MOV, WEBM</p>
            )}
          </div>
        )}
        <input
          ref={inputRef}
          type="file"
          accept={VIDEO_ACCEPT}
          className="hidden"
          disabled={disabled}
          onChange={(event) => {
            acceptFile(event.target.files?.[0]);
            event.target.value = "";
          }}
        />
      </div>
      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        Your file is transferred directly to private storage and verified server-side.
      </p>
    </div>
  );
}
