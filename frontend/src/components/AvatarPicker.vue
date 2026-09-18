<template>
  <!-- 头像选择面板：内置 SVG 头像网格 + 恢复默认。
       只负责「选哪个」，落库由父组件（个人主页）调接口完成。 -->
  <div class="avatar-picker">
    <div class="preset-grid">
      <button
        v-for="preset in presets"
        :key="preset.id"
        type="button"
        class="preset-item"
        :class="{ 'is-current': preset.url === current }"
        :title="preset.name"
        :disabled="saving"
        @click="$emit('select', preset.url)"
      >
        <img class="preset-img" :src="preset.url" :alt="preset.name" />
      </button>
    </div>

    <div class="picker-footer">
      <span class="current-hint">{{ currentLabel }}</span>
      <el-button
        text
        type="primary"
        :disabled="saving || !current"
        @click="$emit('reset')"
      >
        恢复默认
      </el-button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  // [{ id, name, url }]，来自 GET /api/profile/avatars
  presets: { type: Array, default: () => [] },
  // 当前用户的 avatar_url；为空字符串 / null 表示默认头像
  current: { type: String, default: '' },
  saving: { type: Boolean, default: false },
})

defineEmits(['select', 'reset'])

// 「恢复默认」＝把 avatar_url 清空，所以只有「本来有头像」时才可点；
// 提示语分三种：内置头像 / 默认头像 / 历史头像（上传或外链的老数据）。
const currentIsPreset = computed(() =>
  props.presets.some((preset) => preset.url === props.current)
)

const currentLabel = computed(() => {
  if (currentIsPreset.value) return '当前：内置头像'
  if (!props.current) return '当前：默认头像（显示名首字符）'
  return '当前：历史头像，选一个上面的内置头像即可替换'
})
</script>

<style scoped>
.preset-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(64px, 1fr));
  gap: 14px;
}

/* 头像本身就是按钮：整块可点，选中态描一圈主题色 */
.preset-item {
  padding: 3px;
  border: 2px solid transparent;
  border-radius: 50%;
  background: none;
  cursor: pointer;
  line-height: 0;
  transition: border-color 0.2s, transform 0.2s;
}

.preset-item:hover:not(:disabled) {
  transform: scale(1.06);
}

.preset-item:disabled {
  cursor: default;
}

.preset-item.is-current {
  border-color: #409eff;
}

.preset-img {
  width: 100%;
  aspect-ratio: 1;
  border-radius: 50%;
  display: block;
}

.picker-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 16px;
}

.current-hint {
  font-size: 13px;
  color: var(--app-text-muted);
}
</style>
