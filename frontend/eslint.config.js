// ESLint 9 flat config：Vue 3 SFC + 浏览器/Node 环境
// 运行：npm run lint（= eslint .）
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'

export default [
  {
    ignores: ['dist/**', 'node_modules/**', '*.min.js'],
  },
  js.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    rules: {
      // 项目约定
      'vue/multi-word-component-names': 'off', // HomeView / LoginView 等命名允许
      // 模板排版类规则全部关闭（不强制属性顺序 / 换行 / 缩进风格），
      // 只保留 essential + recommended 里的功能性检查（如重复属性、破坏性指令）
      'vue/attributes-order': 'off',
      'vue/first-attribute-linebreak': 'off',
      'vue/html-indent': 'off',
      'vue/html-closing-bracket-newline': 'off',
      'vue/max-attributes-per-line': 'off',
      'vue/multiline-html-element-content-newline': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
      // 空 catch（刻意吞错）允许；catch 参数不参与 unused 检查
      'no-empty': ['error', { allowEmptyCatch: true }],
      'no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        caughtErrors: 'none',
        varsIgnorePattern: '^_',
      }],
    },
  },
  {
    // Vite / 配置类 Node 脚本
    files: ['vite.config.js', 'eslint.config.js'],
    languageOptions: {
      globals: { process: 'readonly' },
    },
  },
]
