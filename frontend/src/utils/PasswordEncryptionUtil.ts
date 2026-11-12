import * as smCrypto from 'sm-crypto'

// Base64 工具函数（添加类型注解和 JSDoc）
/**
 * Base64 编码
 * @param str - 需要编码的字符串
 * @returns Base64 编码字符串
 */
const base64Encode = (str: string): string =>
  btoa(unescape(encodeURIComponent(str)))

/**
 * Base64 解码
 * @param str - 需要解码的 Base64 字符串
 * @returns 解码后的原始字符串
 */
const base64Decode = (str: string): string =>
  decodeURIComponent(escape(atob(str)))

// 算法上下文接口（添加泛型约束）
interface AlgorithmContext<T extends string> {
  encrypt: (plaintext: string, key: T) => string
  decrypt: (ciphertext: string, key: T) => string
}

// 支持的算法类型（使用 const enum 更严格）
type SupportedAlgorithms = 'SM2' | 'SM4' | 'NONE'

// 算法注册表（使用立即执行函数初始化）
const algorithmRegistry: Record<SupportedAlgorithms, AlgorithmContext<string>> = (() => {
  const cipherMode = 0
  const registry: Record<SupportedAlgorithms, AlgorithmContext<string>> = {
    SM2: {
      encrypt: (plaintext, publicKey) => {
        const ciphertext = smCrypto.sm2.doEncrypt(plaintext, publicKey, cipherMode)
        const encoded = base64Encode(ciphertext)
        return encoded
      },
      decrypt: (ciphertext, privateKey) => {
        const decoded = base64Decode(ciphertext)
        const result = smCrypto.sm2.doDecrypt(decoded, privateKey, cipherMode) // 修正为SM2解密
        return result
      },
    },
    SM4: {
      encrypt: (plaintext, key) => {
        const ciphertext = smCrypto.sm4.encrypt(plaintext, key)
        const encoded = base64Encode(ciphertext)

        return encoded
      },
      decrypt: (ciphertext, key) => {
        try {
          const decoded = base64Decode(ciphertext)
          const result = smCrypto.sm4.decrypt(decoded, key)

          return result
        }
        catch (error) {
          console.error(`[SM4解密] 错误: ${error}`)
          throw error
        }
      },
    },
    NONE: {
      encrypt: (plaintext) => {
        return plaintext
      },
      decrypt: (ciphertext) => {
        return ciphertext
      },
    },
  }

  return registry
})()

/**
 * 通用加密函数（增强类型安全）
 * @param plaintext - 明文字符串
 * @param algorithm - 算法名称（SM2/SM4/NONE）
 * @param key - 密钥（SM4用对称密钥，SM2用公钥，NONE不需要）
 * @returns Base64 包装的密文（NONE算法返回原始明文）
 */
export function encrypt(
  plaintext: string,
  algorithm: SupportedAlgorithms,
  key: string,
): string {
  const algo = algorithmRegistry[algorithm.toUpperCase() as SupportedAlgorithms]

  if (!algo) {
    throw new Error(`Unsupported algorithm: ${algorithm}`)
  }

  return algo.encrypt(plaintext, key)
}

/**
 * 通用解密函数（增强类型安全）
 * @param ciphertextBase64 - Base64 包装的密文（NONE算法直接传入原始密文）
 * @param algorithm - 算法名称（SM2/SM4/NONE）
 * @param key - 密钥（SM4用对称密钥，SM2用私钥，NONE不需要）
 * @returns 解密后的明文（NONE算法返回原始密文）
 */
export function decrypt(
  ciphertextBase64: string,
  algorithm: SupportedAlgorithms,
  key: string,
): string {
  const algo = algorithmRegistry[algorithm.toUpperCase() as SupportedAlgorithms]

  if (!algo) {
    throw new Error(`Unsupported algorithm: ${algorithm}`)
  }

  return algo.decrypt(ciphertextBase64, key)
}

/**
 * SM3 哈希算法（单独处理非对称场景）
 * @param txt - 需要哈希的字符串
 * @returns Base64 包装的哈希值
 */
export function hashSM3(txt: string): string {
  return base64Encode(smCrypto.sm3.hash(txt))
}

// 类型守卫函数（增强类型安全）
export function isSupportedAlgorithm(algorithm: string): algorithm is SupportedAlgorithms {
  return ['SM2', 'SM4', 'NONE'].includes(algorithm.toUpperCase())
}
