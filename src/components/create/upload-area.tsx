"use client";

import * as React from "react";
import { FileVideo, Upload, X } from "lucide-react";

import type { UploadedFileMeta } from "@/types";
import { cn, formatBytes } from "@/lib/utils";

interface UploadAreaProps {
  file: UploadedFileMeta | null;
  onFileSelected: (file: UploadedFileMeta) => void;
  onFileCleared: () => void;
  error?: string | null;
}

/**
 * Drag-and-drop video upload area.
 *
 * INTEGRATION POINT (Phase 3): files are currently only read as metadata in
 * the browser — nothing is transferred over the network. The multipart upload
 * to the worker (POST /api/v1/uploads) will replace this in Phase 3.
 */
export function UploadArea({ file, onFileSelected, onFileCleared, error }: UploadAreaProps) {
  const [dragActive, setDragActive] = React.useState(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  function acceptFiles(files: FileList | null) {
    const first = files?.[0];
    if (!first) return;
    if (!first.type.startsWith("video/")) {
      // The error is surfaced by the parent via `error`; report it upward
      // by selecting nothing and letting validation handle the message.
      onFileCleared();
      return;
    }
    onFileSelected({ name: first.name, sizeBytes: first.size });
  }

  return (
    <div className="flex flex-col gap-2">
      <div
        role="button"
        tabIndex={0}
        aria-label="Upload a video file"
        onClick={() => inputRef.current?.click()}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          setDragActive(true);
        }}
        onDragLeave={() => setDragActive(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragActive(false);
          acceptFiles(event.dataTransfer.files);
        }}
        className={cn(
          "flex cursor-pointer flex-col items-center justify-center gap-3 rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors",
          dragActive ? "border-primary bg-primary/10" : "border-input bg-background/40 hover:border-primary/50",
          error && "border-destructive"
        )}
      >
        <span className="flex h-11 w-11 items-center justify-center rounded-full bg-accent text-accent-foreground">
          {file ? <FileVideo size={20} /> : <Upload size={20} />}
        </span>
        {file ? (
          <div className="flex items-center gap-2 text-sm">
            <span className="font-medium">{file.name}</span>
            <span className="text-muted-foreground">({formatBytes(file.sizeBytes)})</span>
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
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            <p className="text-sm font-medium">Drag and drop a video file</p>
            <p className="text-xs text-muted-foreground">or click to browse — MP4, MOV, WebM</p>
          </div>
        )}
        <input
          ref={inputRef}
          type="file"
          accept="video/*"
          className="hidden"
          onChange={(event) => {
            acceptFiles(event.target.files);
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
        Phase 1 note: the file is held in memory for the UI only and is not uploaded yet.
      </p>
    </div>
  );
}
