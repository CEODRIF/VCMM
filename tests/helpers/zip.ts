/**
 * Minimal ZIP reader for tests (EOCD → central directory → local entries).
 *
 * Verifies the binary layout produced by src/lib/zip/store-zip.ts:
 * signatures, STORE method, CRCs, sizes, offsets, UTF-8 names and
 * data-descriptor placement. Shared by the engine unit tests and the
 * download-all route tests (the route's streamed bytes must parse as a
 * real ZIP with the right entries).
 */
import { expect } from "vitest";

export interface ParsedEntry {
  name: string;
  method: number;
  crc: number;
  size: number;
  offset: number;
  data: Uint8Array;
  utf8Flag: boolean;
  /** General-purpose flag bits as written in the CENTRAL directory. */
  centralFlags: number;
  /** General-purpose flag bits as written in the LOCAL file header. */
  localFlags: number;
}

export interface ParsedZip {
  entries: ParsedEntry[];
  totalBytes: number;
}

const decoder = new TextDecoder("utf-8");

export function parseZip(bytes: Uint8Array): ParsedZip {
  // EOCD: search backwards for the signature (no comment is written).
  let eocd = -1;
  for (let i = bytes.length - 22; i >= 0; i -= 1) {
    if (bytes[i] === 0x50 && bytes[i + 1] === 0x4b && bytes[i + 2] === 0x05 && bytes[i + 3] === 0x06) {
      eocd = i;
      break;
    }
  }
  expect(eocd, "EOCD not found").toBeGreaterThanOrEqual(0);
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const entryCount = dv.getUint16(eocd + 10, true);
  const centralOffset = dv.getUint32(eocd + 16, true);
  const centralSize = dv.getUint32(eocd + 12, true);
  // centralSize covers the central directory only (the 22-byte EOCD follows it)
  expect(centralSize).toBe(bytes.length - centralOffset - 22);

  const entries: ParsedEntry[] = [];
  let pos = centralOffset;
  for (let i = 0; i < entryCount; i += 1) {
    expect(dv.getUint32(pos, true), `central sig @${pos}`).toBe(0x02014b50);
    const method = dv.getUint16(pos + 10, true);
    const flags = dv.getUint16(pos + 8, true);
    const crc = dv.getUint32(pos + 16, true);
    const size = dv.getUint32(pos + 20, true);
    const nameLen = dv.getUint16(pos + 28, true);
    const offset = dv.getUint32(pos + 42, true);
    const name = decoder.decode(bytes.subarray(pos + 46, pos + 46 + nameLen));
    pos += 46 + nameLen;

    // local header
    expect(dv.getUint32(offset, true), `local sig @${offset}`).toBe(0x04034b50);
    const localFlags = dv.getUint16(offset + 6, true);
    expect(localFlags & 0x08, "data-descriptor bit must be set").toBe(0x08);
    const localNameLen = dv.getUint16(offset + 26, true);
    const localExtraLen = dv.getUint16(offset + 28, true);
    const dataStart = offset + 30 + localNameLen + localExtraLen;
    const data = bytes.slice(dataStart, dataStart + size);
    // data descriptor immediately after the data
    const desc = dataStart + size;
    expect(dv.getUint32(desc, true), `descriptor sig @${desc}`).toBe(0x08074b50);
    expect(dv.getUint32(desc + 4, true), "descriptor crc").toBe(crc);
    expect(dv.getUint32(desc + 8, true), "descriptor csize").toBe(size);

    entries.push({
      name,
      method,
      crc,
      size,
      offset,
      data,
      utf8Flag: (flags & 0x0800) !== 0,
      centralFlags: flags,
      localFlags,
    });
  }
  return { entries, totalBytes: bytes.length };
}
