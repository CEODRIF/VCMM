/**
 * Unit tests for the streaming STORE-ZIP writer (Phase 6).
 *
 * The archive is parsed back by a minimal ZIP reader written here (EOCD →
 * central directory → local entries), so the tests verify the actual binary
 * layout: signatures, stored method, CRCs, sizes, offsets, UTF-8 names,
 * data-descriptor placement and byte-for-byte data.
 */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { Crc32, streamStoreZip, type StoreZipEntry } from "@/lib/zip/store-zip";
import { parseZip } from "./helpers/zip";

// -- helpers -----------------------------------------------------------------------

async function* chunksOf(data: Uint8Array, size = 3): AsyncGenerator<Uint8Array> {
  let i = 0;
  while (i < data.length) {
    const c = data.slice(i, i + size);
    i += size;
    yield c;
  }
}
async function* one(data: Uint8Array): AsyncGenerator<Uint8Array> {
  yield data;
}

const enc = new TextEncoder();
async function build(entries: StoreZipEntry[]): Promise<Uint8Array> {
  const out: number[] = [];
  for await (const c of streamStoreZip(entries)) {
    for (const b of c) out.push(b);
  }
  return new Uint8Array(out);
}

// -- tests ----------------------------------------------------------------------------

describe("Crc32", () => {
  it("matches the standard check value for '123456789'", () => {
    const c = new Crc32();
    c.update(enc.encode("123456789"));
    expect(c.value()).toBe(0xcbf43926);
  });

  it("is incremental (chunked == whole)", () => {
    const whole = new Crc32();
    whole.update(enc.encode("hello world, streaming crc"));
    const a = new Crc32();
    a.update(enc.encode("hello "));
    a.update(enc.encode("world, "));
    a.update(enc.encode("streaming crc"));
    expect(a.value()).toBe(whole.value());
  });
});

describe("streamStoreZip", () => {
  it("produces a valid empty ZIP (EOCD only)", async () => {
    const parsed = parseZip(await build([]));
    expect(parsed.entries).toHaveLength(0);
    expect(parsed.totalBytes).toBe(22);
  });

  it("round-trips a single chunked entry (stored, CRC, data intact)", async () => {
    const payload = enc.encode("0123456789abcdef");
    const parsed = parseZip(await build([{ name: "clip.mp4", source: chunksOf(payload, 5) }]));
    expect(parsed.entries).toHaveLength(1);
    const e = parsed.entries[0]!;
    expect(e.name).toBe("clip.mp4");
    expect(e.method).toBe(0); // STORED
    expect(e.utf8Flag).toBe(true);
    expect(e.size).toBe(payload.length);
    const crc = new Crc32();
    crc.update(payload);
    expect(e.crc).toBe(crc.value());
    expect(Array.from(e.data)).toEqual(Array.from(payload));
  });

  it("round-trips multiple entries with correct offsets", async () => {
    const a = enc.encode("alpha-".repeat(50));
    const b = enc.encode("bravo-".repeat(37));
    const c = new Uint8Array(0); // empty entry is legal
    const parsed = parseZip(
      await build([
        { name: "a.mp4", source: chunksOf(a, 7) },
        { name: "b.mp4", source: one(b) },
        { name: "c.mp4", source: chunksOf(c, 4) },
      ])
    );
    expect(parsed.entries.map((e) => e.name)).toEqual(["a.mp4", "b.mp4", "c.mp4"]);
    expect(parsed.entries[0]!.data).toEqual(a);
    expect(parsed.entries[1]!.data).toEqual(b);
    expect(parsed.entries[2]!.size).toBe(0);
    const offsets = parsed.entries.map((e) => e.offset);
    expect(offsets[1]!).toBeGreaterThan(offsets[0]!);
    expect(offsets[2]!).toBeGreaterThan(offsets[1]!);
  });

  it("encodes UTF-8 names (Arabic + umlauts) and decodes them back", async () => {
    const names = ["مرحبا بالعالم.mp4", "Grüße Straße.mp4", "clip-3.mp4"];
    const parsed = parseZip(
      await build(names.map((n) => ({ name: n, source: one(enc.encode("x")) })))
    );
    expect(parsed.entries.map((e) => e.name)).toEqual(names);
    for (const e of parsed.entries) expect(e.utf8Flag).toBe(true);
  });

  it("is byte-for-byte deterministic for identical entries", async () => {
    const mk = (): StoreZipEntry[] => [
      { name: "one.mp4", source: one(enc.encode("same")) },
      { name: "two.mp4", source: chunksOf(enc.encode("bytes"), 2) },
    ];
    const first = await build(mk());
    const second = await build(mk());
    expect(Array.from(second)).toEqual(Array.from(first));
  });

  it("rejects impossible entry names before yielding any data", async () => {
    const gen = streamStoreZip([{ name: "x".repeat(70000), source: one(enc.encode("x")) }]);
    await expect(gen.next()).rejects.toThrow(RangeError);
  });

  it("propagates source errors (caller aborts + cleans up)", async () => {
    async function* boom(): AsyncGenerator<Uint8Array> {
      yield enc.encode("partial");
      throw new Error("object vanished mid-stream");
    }
    const gen = streamStoreZip([{ name: "a.mp4", source: boom() }]);
    await gen.next(); // local header
    await gen.next(); // the "partial" chunk
    await expect(gen.next()).rejects.toThrow("object vanished mid-stream");
  });

  it("accumulates sizes correctly across many chunks (descriptor == central)", async () => {
    const payload = new Uint8Array(100_000);
    for (let i = 0; i < payload.length; i += 1) payload[i] = (i * 7) & 0xff;
    const parsed = parseZip(await build([{ name: "big.mp4", source: chunksOf(payload, 999) }]));
    expect(parsed.entries[0]!.size).toBe(payload.length);
    expect(parsed.entries[0]!.data).toEqual(payload);
  });

  it("writes IDENTICAL general-purpose flags in the local header and the central directory", async () => {
    const parsed = parseZip(
      await build([
        { name: "clip-a.mp4", source: chunksOf(enc.encode("payload-a"), 3) },
        { name: "مرحبا بالعالم.mp4", source: one(enc.encode("payload-b")) },
        { name: "empty.mp4", source: chunksOf(new Uint8Array(0), 2) },
      ])
    );
    expect(parsed.entries.length).toBeGreaterThan(0);
    for (const e of parsed.entries) {
      // bit 3 (data descriptor) + bit 11 (UTF-8) = 0x0808 — in BOTH places.
      expect(e.localFlags, `local flags for ${e.name}`).toBe(0x0808);
      expect(e.centralFlags, `central flags for ${e.name}`).toBe(e.localFlags);
    }
  });

  it("extracts with an INDEPENDENT zip implementation (CPython zipfile: CRC check + extractall)", async () => {
    const py = findPython();
    if (!py) return; // no python interpreter in this environment — skip cleanly (CI runners have one)

    const hello = enc.encode("hello-zip");
    const pattern = new Uint8Array(50_000);
    for (let i = 0; i < pattern.length; i += 1) pattern[i] = (i * 31) & 0xff;
    const entries: StoreZipEntry[] = [
      { name: "hello.txt", source: chunksOf(hello, 4) },
      { name: "data.bin", source: chunksOf(pattern, 777) },
      { name: "empty.txt", source: chunksOf(new Uint8Array(0), 1) },
    ];

    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "zipforge-"));
    const zipPath = path.join(dir, "archive.zip");
    fs.writeFileSync(zipPath, await build(entries));
    try {
      const scriptPath = path.join(dir, "check.py");
      fs.writeFileSync(scriptPath, PY_CHECK_SCRIPT);
      const out = execFileSync(py, [scriptPath, zipPath], { encoding: "utf-8" });
      const lines = out.trim().split("\n");
      expect(lines[lines.length - 1]).toBe("OK");
      const expectedNames = ["hello.txt", "data.bin", "empty.txt"];
      const expectedSizes = [hello.length, pattern.length, 0];
      const expectedHashes = [sha256Short(hello), sha256Short(pattern), sha256Short(new Uint8Array(0))];
      for (let i = 0; i < expectedNames.length; i += 1) {
        // name<TAB>flag_bits (2056 == 0x0808, from the CENTRAL directory)<TAB>size<TAB>sha256[:12]
        expect(lines[i]).toBe(`${expectedNames[i]}\t2056\t${expectedSizes[i]}\t${expectedHashes[i]}`);
      }
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

// -- independent-extraction helpers (CPython zipfile, zero extra deps) -------------

function findPython(): string | undefined {
  for (const candidate of ["python3", "python"]) {
    try {
      execFileSync(candidate, ["--version"], { stdio: "ignore" });
      return candidate;
    } catch {
      // try the next candidate
    }
  }
  return undefined;
}

function sha256Short(data: Uint8Array): string {
  return createHash("sha256").update(data).digest("hex").slice(0, 12);
}

/** Runs with the generated archive as argv[1]; asserts CRCs + real extraction. */
const PY_CHECK_SCRIPT = `
import hashlib
import os
import sys
import tempfile
import zipfile

path = sys.argv[1]
with zipfile.ZipFile(path) as z:
    bad = z.testzip()
    assert bad is None, f"CRC error in {bad}"
    tmp = tempfile.mkdtemp()
    z.extractall(tmp)
    for info in z.infolist():
        data = z.read(info.filename)
        on_disk = open(os.path.join(tmp, info.filename), "rb").read()
        assert data == on_disk, f"extraction mismatch: {info.filename}"
        digest = hashlib.sha256(data).hexdigest()[:12]
        print(f"{info.filename}\\t{info.flag_bits}\\t{len(data)}\\t{digest}")
print("OK")
`;
