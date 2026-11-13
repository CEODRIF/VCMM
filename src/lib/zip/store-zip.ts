/**
 * Streaming STORE-method ZIP writer (Phase 6 "Download all").
 *
 * Why hand-rolled:
 * - ZIP entries are stored UNCOMPRESSED (method 0): MP4s are already
 *   compressed, so compression would cost CPU for ~0 bytes saved.
 * - Zero dependencies: no archiver needed on the serverless edge.
 * - TRUE streaming: entries are async iterables; each chunk is passed
 *   through while an incremental CRC32 + size counter runs, so memory
 *   stays at chunk size (a few KB) no matter how large the videos are.
 *   The central directory is assembled at the end from the per-entry
 *   facts (name, CRC, size, offset) — everything else is written once.
 *
 * Format notes (PKZIP appnote):
 * - Bit 3 of the local header's general-purpose flag is set, so CRC/size
 *   are NOT needed in the local header and are written in a data
 *   descriptor (0x08074b50) directly after the entry data — this is what
 *   makes streaming possible.
 * - Bit 11 is set (names are UTF-8), which also covers non-ASCII clip
 *   titles; no extra fields are written.
 * - ZIP64 is intentionally NOT supported: callers must cap the total
 *   size below 4 GiB (the route enforces a much smaller cap).
 *
 * The function is a pure byte producer (no I/O, no clock, no randomness):
 * given the same entries it always yields the same bytes, which keeps the
 * behavior deterministic and unit-testable.
 */

export interface StoreZipEntry {
  /** Entry name. Must be pre-sanitized by the caller (no '/', '..'). */
  name: string;
  /** Data source — streamed chunk by chunk, never buffered whole. */
  source: AsyncIterable<Uint8Array>;
}

export interface StreamedEntryFacts {
  name: string;
  size: number;
  crc: number;
}

// -- CRC32 (IEEE 802.3, polynomial 0xEDB88320) --------------------------------

const CRC_TABLE = new Uint32Array(256);
for (let n = 0; n < 256; n += 1) {
  let c = n;
  for (let k = 0; k < 8; k += 1) {
    c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  }
  CRC_TABLE[n] = c >>> 0;
}

export class Crc32 {
  private state = 0xffffffff;

  update(chunk: Uint8Array): void {
    let c = this.state;
    for (const b of chunk) {
      c = CRC_TABLE[(c ^ b) & 0xff]! ^ (c >>> 8); // index is masked to 0-255
    }
    this.state = c >>> 0;
  }

  value(): number {
    return (this.state ^ 0xffffffff) >>> 0;
  }
}

// -- binary helpers -------------------------------------------------------------

const LE = new DataView(new ArrayBuffer(8));

function u16(v: number): Uint8Array {
  LE.setUint16(0, v & 0xffff, true);
  return new Uint8Array(LE.buffer.slice(0, 2));
}

function u32(v: number): Uint8Array {
  LE.setUint32(0, v >>> 0, true);
  return new Uint8Array(LE.buffer.slice(0, 4));
}

function concat(chunks: Uint8Array[]): Uint8Array {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
}

const UTF8 = new TextEncoder();
const DOS_DATE = ((1980 - 1980) << 9) | (1 << 5) | 1; // 1980-01-01 (fixed: deterministic output)
const DOS_TIME = 0; // 00:00:00

// -- the writer -------------------------------------------------------------------

const LOCAL_HEADER_SIG = 0x04034b50;
const DESCRIPTOR_SIG = 0x08074b50;
const CENTRAL_SIG = 0x02014b50;
const EOCD_SIG = 0x06054b50;

function localHeader(name: Uint8Array): Uint8Array {
  // version needed 2.0, flags: bit3 (data descriptor) + bit11 (UTF-8) = 0x0808
  return concat([
    u32(LOCAL_HEADER_SIG),
    u16(20),
    u16(0x0808),
    u16(0), // method: stored
    u16(DOS_TIME),
    u16(DOS_DATE),
    u32(0), // crc — filled by the data descriptor
    u32(0), // compressed size — filled by the descriptor
    u32(0), // uncompressed size — filled by the descriptor
    u16(name.length),
    u16(0), // no extra field
    name,
  ]);
}

function descriptor(crc: number, size: number): Uint8Array {
  return concat([u32(DESCRIPTOR_SIG), u32(crc), u32(size), u32(size)]);
}

function centralRecord(name: Uint8Array, crc: number, size: number, offset: number): Uint8Array {
  return concat([
    u32(CENTRAL_SIG),
    u16(20), // version made by
    u16(20), // version needed
    u16(0x0800), // flags (UTF-8 bit)
    u16(0), // method: stored
    u16(DOS_TIME),
    u16(DOS_DATE),
    u32(crc),
    u32(size),
    u32(size),
    u16(name.length),
    u16(0), // extra len
    u16(0), // comment len
    u16(0), // disk number
    u16(0), // internal attrs
    u32(0), // external attrs
    u32(offset),
    name,
  ]);
}

function eocd(entryCount: number, centralSize: number, centralOffset: number): Uint8Array {
  return concat([
    u32(EOCD_SIG),
    u16(0), // disk
    u16(0), // central dir disk
    u16(entryCount),
    u16(entryCount),
    u32(centralSize),
    u32(centralOffset),
    u16(0), // no comment
  ]);
}

/**
 * Streams the bytes of a STORE-method ZIP for the given entries, in order.
 *
 * Guarantees:
 * - yields chunks as they are read from each entry (bounded memory);
 * - yields the complete central directory + EOCD at the end;
 * - throws `RangeError` (before yielding any data) when an entry would
 *   exceed the 4 GiB ZIP limit — callers must cap sizes anyway;
 * - propagates any error from an entry's source (the caller is then
 *   responsible for aborting the HTTP response / cleanup).
 */
export async function* streamStoreZip(entries: StoreZipEntry[]): AsyncGenerator<Uint8Array> {
  if (entries.length === 0) {
    // A valid empty ZIP is just the central directory (empty) + EOCD.
    yield eocd(0, 0, 0);
    return;
  }

  const central: Uint8Array[] = [];
  let offset = 0;

  for (const entry of entries) {
    const name = UTF8.encode(entry.name);
    // Fail fast (before yielding ANY bytes) on impossible entries.
    if (name.length === 0 || name.length > 0xffff) {
      throw new RangeError(`ZIP entry name has an invalid length: ${entry.name}`);
    }

    const header = localHeader(name);
    const entryOffset = offset; // local-header position in the final archive
    yield header;
    offset += header.length;

    const crc = new Crc32();
    let size = 0;
    for await (const chunk of entry.source) {
      if (chunk.length === 0) continue;
      size += chunk.length;
      if (size > 0xffffffff) {
        throw new RangeError(`ZIP entry "${entry.name}" exceeds the 4 GiB limit`);
      }
      crc.update(chunk);
      yield chunk;
      offset += chunk.length;
    }

    const desc = descriptor(crc.value(), size);
    yield desc;
    offset += desc.length;

    central.push(centralRecord(name, crc.value(), size, entryOffset));
  }

  const centralOffset = offset;
  const centralBytes = concat(central);
  yield centralBytes;
  yield eocd(entries.length, centralBytes.length, centralOffset);
}
