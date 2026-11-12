import { describe, expect, it } from 'vitest'
import { IncrementalSha256, sha256Blob, sha256Bytes } from './sha256.ts'

const encoder = new TextEncoder()

describe('incremental SHA-256', () => {
  it('matches the standard empty and abc vectors', () => {
    expect(sha256Bytes(new Uint8Array())).toBe(
      'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    )
    expect(sha256Bytes(encoder.encode('abc'))).toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    )
  })

  it('keeps the same digest across arbitrary update boundaries', () => {
    const digest = new IncrementalSha256()
    digest.update(encoder.encode('The quick brown '))
    digest.update(encoder.encode('fox jumps over the lazy dog'))
    expect(digest.hex()).toBe(
      'd7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592',
    )
  })

  it('hashes a Blob incrementally', async () => {
    const value = new Blob([encoder.encode('abc'.repeat(10_000))])
    await expect(sha256Blob(value, { sliceSize: 257 })).resolves.toBe(
      '13b77af908a78a94f2e21cf8fc137ea16c8020873eeee7b6b96b6b0975555a02',
    )
  })
})
