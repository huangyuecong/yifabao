import path from 'node:path'
import { fileURLToPath } from 'node:url'
import antfu from '@antfu/eslint-config'
import { FlatCompat } from '@eslint/eslintrc'
import nounsanitized from 'eslint-plugin-no-unsanitized'

// mimic CommonJS variables -- not needed if using CommonJS
const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

const compat = new FlatCompat({
  baseDirectory: __dirname,
})

export default antfu(
  {
    formatters: true,
    lessOpinionated: true,
    unocss: true,
    ignores: [
      '**/node_modules/**',
      '**/dist/**',
      // '**/src/components/**',
      '**/src/hooks/**',
      '**/*.d.ts',
      '**/build/**',
      '**/src/main.ts',
      '**/config/**',
    ],

  },

  {
    // Remember to specify the file glob here, otherwise it might cause the vue plugin to handle non-vue files
    files: ['**/*.vue'],
    rules: {
      'no-use-before-define': 'off',
      '@typescript-eslint/no-use-before-define': 'off',
      'unused-imports/no-unused-vars': 'off',
      '@typescript-eslint/unused-imports/no-unused-vars': 'off',
    },
  },
  {
    rules: {
      'brace-style': ['error', 'stroustrup', { allowSingleLine: false }],
      'curly': ['error', 'all'],
      'no-console': ['warn', { allow: ['info', 'warn', 'error'] }],
      'vue/max-attributes-per-line': ['error', { singleline: 3 }],
      'vue/max-len': ['error', {
        code: 160,
        ignoreComments: true,
        ignoreHTMLAttributeValues: true,
        ignoreHTMLTextContents: true,
        ignoreRegExpLiterals: true,
        ignoreStrings: true,
        ignoreTemplateLiterals: true,
        ignoreUrls: true,
      }],
      'vue/singleline-html-element-content-newline': 'off',
    },
    languageOptions: {
      globals: {

      },
    },
  },

  [nounsanitized.configs.recommended], // 代码安全性检测
  // mimic ESLintRC-style extends
  ...compat.extends('plugin:financial/recommended'), // 避免财务计算
)
