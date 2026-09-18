<template>
  <!-- 编辑态的正文框 + 取消/保存，详情页卡片与个人主页共用。
       上面那颗「引子 + 可点的星星」由调用方通过默认插槽放进来说明改的是哪一条
       （详情页放署名、个人主页放片名），两边那段样式本来就不同，不强行拉平。 -->
  <div class="edit-form">
    <slot />
    <el-input
      v-model="text"
      type="textarea"
      :rows="rows"
      :placeholder="placeholder"
      maxlength="500"
      show-word-limit
    />
    <div class="edit-actions">
      <el-button size="small" :disabled="saving" @click="$emit('cancel')">取消</el-button>
      <el-button
        size="small"
        type="primary"
        :loading="saving"
        :disabled="!text.trim()"
        @click="$emit('save')"
      >
        保存
      </el-button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  modelValue:  { type: String,  default: '' },
  rows:        { type: Number,  default: 4 },
  placeholder: { type: String,  default: '' },
  saving:      { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'cancel', 'save'])

const text = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})
</script>

<style scoped>
.edit-actions {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
