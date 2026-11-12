/* eslint-disable antfu/consistent-list-newline, financial/no-division */
const INITIAL_STATE = new Uint32Array([
  0x6A09E667,
  0xBB67AE85,
  0x3C6EF372,
  0xA54FF53A,
  0x510E527F,
  0x9B05688C,
  0x1F83D9AB,
  0x5BE0CD19,
])

const ROUND_CONSTANTS = new Uint32Array([
  0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5,
  0x3956C25B, 0x59F111F1, 0x923F82A4, 0xAB1C5ED5,
  0xD807AA98, 0x12835B01, 0x243185BE, 0x550C7DC3,
  0x72BE5D74, 0x80DEB1FE, 0x9BDC06A7, 0xC19BF174,
  0xE49B69C1, 0xEFBE4786, 0x0FC19DC6, 0x240CA1CC,
  0x2DE92C6F, 0x4A7484AA, 0x5CB0A9DC, 0x76F988DA,
  0x983E5152, 0xA831C66D, 0xB00327C8, 0xBF597FC7,
  0xC6E00BF3, 0xD5A79147, 0x06CA6351, 0x14292967,
  0x27B70A85, 0x2E1B2138, 0x4D2C6DFC, 0x53380D13,
  0x650A7354, 0x766A0ABB, 0x81C2C92E, 0x92722C85,
  0xA2BFE8A1, 0xA81A664B, 0xC24B8B70, 0xC76C51A3,
  0xD192E819, 0xD6990624, 0xF40E3585, 0x106AA070,
  0x19A4C116, 0x1E376C08, 0x2748774C, 0x34B0BCB5,
  0x391C0CB3, 0x4ED8AA4A, 0x5B9CCA4F, 0x682E6FF3,
  0x748F82EE, 0x78A5636F, 0x84C87814, 0x8CC70208,
  0x90BEFFFA, 0xA4506CEB, 0xBEF9A3F7, 0xC67178F2,
])

function rotateRight(value: number, amount: number) {
  return (value >>> amount) | (value << (32 - amount))
}

function abortError() {
  return new DOMException('上传已取消', 'AbortError')
}

export class IncrementalSha256 {
  private readonly state = new Uint32Array(INITIAL_STATE)
  private readonly block = new Uint8Array(64)
  private readonly words = new Uint32Array(64)
  private blockLength = 0
  private bytesHashed = 0
  private finished = false

  update(data: Uint8Array) {
    if (this.finished) {
      throw new Error('SHA-256 已完成，不能继续写入')
    }
    this.bytesHashed += data.byteLength
    let offset = 0
    while (offset < data.byteLength) {
      const available = 64 - this.blockLength
      const count = Math.min(available, data.byteLength - offset)
      this.block.set(data.subarray(offset, offset + count), this.blockLength)
      this.blockLength += count
      offset += count
      if (this.blockLength === 64) {
        this.compress()
        this.blockLength = 0
      }
    }
    return this
  }

  digest() {
    if (!this.finished) {
      this.finish()
    }
    const output = new Uint8Array(32)
    const view = new DataView(output.buffer)
    this.state.forEach((value, index) => view.setUint32(index * 4, value, false))
    return output
  }

  hex() {
    return toHex(this.digest())
  }

  private finish() {
    this.finished = true
    this.block[this.blockLength++] = 0x80
    if (this.blockLength > 56) {
      this.block.fill(0, this.blockLength)
      this.compress()
      this.blockLength = 0
    }
    this.block.fill(0, this.blockLength, 56)
    const bitHigh = Math.floor(this.bytesHashed / 0x20000000)
    const bitLow = (this.bytesHashed * 8) >>> 0
    const view = new DataView(this.block.buffer)
    view.setUint32(56, bitHigh, false)
    view.setUint32(60, bitLow, false)
    this.compress()
    this.blockLength = 0
  }

  private compress() {
    const view = new DataView(this.block.buffer, this.block.byteOffset, this.block.byteLength)
    for (let index = 0; index < 16; index += 1) {
      this.words[index] = view.getUint32(index * 4, false)
    }
    for (let index = 16; index < 64; index += 1) {
      const prior15 = this.words[index - 15]!
      const prior2 = this.words[index - 2]!
      const sigma0 = rotateRight(prior15, 7) ^ rotateRight(prior15, 18) ^ (prior15 >>> 3)
      const sigma1 = rotateRight(prior2, 17) ^ rotateRight(prior2, 19) ^ (prior2 >>> 10)
      this.words[index] = (this.words[index - 16]! + sigma0 + this.words[index - 7]! + sigma1) >>> 0
    }

    let a = this.state[0]!
    let b = this.state[1]!
    let c = this.state[2]!
    let d = this.state[3]!
    let e = this.state[4]!
    let f = this.state[5]!
    let g = this.state[6]!
    let h = this.state[7]!
    for (let index = 0; index < 64; index += 1) {
      const sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25)
      const choose = (e & f) ^ (~e & g)
      const temp1 = (h + sum1 + choose + ROUND_CONSTANTS[index]! + this.words[index]!) >>> 0
      const sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22)
      const majority = (a & b) ^ (a & c) ^ (b & c)
      const temp2 = (sum0 + majority) >>> 0
      h = g
      g = f
      f = e
      e = (d + temp1) >>> 0
      d = c
      c = b
      b = a
      a = (temp1 + temp2) >>> 0
    }
    this.state[0] = (this.state[0]! + a) >>> 0
    this.state[1] = (this.state[1]! + b) >>> 0
    this.state[2] = (this.state[2]! + c) >>> 0
    this.state[3] = (this.state[3]! + d) >>> 0
    this.state[4] = (this.state[4]! + e) >>> 0
    this.state[5] = (this.state[5]! + f) >>> 0
    this.state[6] = (this.state[6]! + g) >>> 0
    this.state[7] = (this.state[7]! + h) >>> 0
  }
}

export function toHex(value: Uint8Array) {
  return Array.from(value, byte => byte.toString(16).padStart(2, '0')).join('')
}

export function sha256Bytes(value: Uint8Array) {
  return new IncrementalSha256().update(value).hex()
}

export async function sha256Blob(
  blob: Blob,
  options: {
    sliceSize?: number
    signal?: AbortSignal
    onProgress?: (completed: number, total: number) => void
  } = {},
) {
  const sliceSize = Math.max(64 * 1024, options.sliceSize || 4 * 1024 * 1024)
  const digest = new IncrementalSha256()
  for (let offset = 0; offset < blob.size; offset += sliceSize) {
    if (options.signal?.aborted) {
      throw abortError()
    }
    const end = Math.min(blob.size, offset + sliceSize)
    digest.update(new Uint8Array(await blob.slice(offset, end).arrayBuffer()))
    options.onProgress?.(end, blob.size)
    await new Promise<void>(resolve => setTimeout(resolve, 0))
  }
  if (options.signal?.aborted) {
    throw abortError()
  }
  return digest.hex()
}

export async function sha256Chunk(blob: Blob) {
  if (globalThis.crypto?.subtle) {
    return toHex(new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', await blob.arrayBuffer())))
  }
  return sha256Blob(blob)
}
