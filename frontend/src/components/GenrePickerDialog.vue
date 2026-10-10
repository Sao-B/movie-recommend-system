<template>
  <el-dialog
    :model-value="visible"
    :title="title"
    width="520px"
    :close-on-click-modal="false"
    :show-close="false"
    @open="syncFromUser"
  >
    <p class="tip">
      选择你喜欢的电影类型（至少 1 个，最多 {{ MAX }} 个），
      新用户推荐会按它来；以后可在个人主页修改。
    </p>
    <div class="genre-cloud">
      <el-check-tag
        v-for="g in genres"
        :key="g"
        :checked="selected.includes(g)"
        class="genre-tag"
        @change="toggle(g)"
      >{{ g }}</el-check-tag>
    </div>
    <p v-if="errorMsg" class="error">{{ errorMsg }}</p>
    <template #footer>
      <el-button @click="skip">跳过，以后再选</el-button>
      <el-button type="primary" :loading="saving" @click="save">
        保存并开始
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, watch } from 'vue'
import { apiMovies } from '@/api/movies'
import { updateUserInfo } from '@/api/profile'
import { useAuthStore } from '@/stores/auth'

const props = defineProps({
  visible: { type: Boolean, default: false },
  /** 弹窗标题（首次引导 / 个人主页编辑共用本组件） */
  title:   { type: String, default: '选择你喜欢的电影类型' },
})
const emit = defineEmits(['close', 'saved'])

const MAX = 5
const auth = useAuthStore()
const genres = ref([])
const selected = ref([])
const saving = ref(false)
const errorMsg = ref('')

// 类型清单来自后端（库里实际存在的类型），不前端写死
apiMovies.genres().then((data) => {
  genres.value = Array.isArray(data) ? data : (data.data || [])
}).catch(() => {})

watch(() => props.visible, (v) => { if (v) syncFromUser() })

function syncFromUser() {
  errorMsg.value = ''
  selected.value = [...(auth.user?.favorite_genres || [])]
}

function toggle(genre) {
  errorMsg.value = ''
  const at = selected.value.indexOf(genre)
  if (at >= 0) {
    selected.value.splice(at, 1)
  } else if (selected.value.length >= MAX) {
    errorMsg.value = `最多选择 ${MAX} 个类型`
  } else {
    selected.value.push(genre)
  }
}

async function persist(list) {
  saving.value = true
  try {
    const data = await updateUserInfo({ favorite_genres: list })
    auth.updateUser(data)
    emit('saved', list)
    emit('close')
  } finally {
    saving.value = false
  }
}

function save() {
  if (!selected.value.length) {
    errorMsg.value = '至少选择 1 个类型（不想选可以点"跳过"）'
    return
  }
  persist([...selected.value])
}

function skip() {
  // 明确跳过 = 存空数组，之后不再弹窗
  persist([])
}
</script>

<style scoped>
.tip        { margin: 0 0 14px; font-size: 14px; color: var(--app-text-muted); }
.genre-cloud{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 10px; }
.genre-tag  { font-size: 14px; }
.error      { margin: 4px 0 0; font-size: 13px; color: var(--el-color-danger); }
</style>
