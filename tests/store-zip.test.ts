/**
 * Unit tests for the streaming STORE-ZIP writer (Phase 6).
 *
 * The archive is parsed back by a minimal ZIP reader written here (EOCD →
 * central directory → local entries), so the tests verify the actual binary
 * layout: signatures, stored method, CRCs, sizes, offsets, UTF-8 names,
 * data-descriptor placement and byte-for-byte data.
 */
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
});
