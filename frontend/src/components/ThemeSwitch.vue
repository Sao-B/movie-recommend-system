<template>
  <div class="tm-wrap" @click.stop>
    <div class="tm" :style="{ height: boxH, transition: tmTrans }">
      <button class="sq cur-sq" :title="MODES[curKey].name + '（点击收起）'" @click="onTop">
        <span class="ic" :style="topStyle"><el-icon><component :is="MODES[curKey].icon" /></el-icon></span>
      </button>
      <button class="sq" :title="MODES[midKey].name" @click="pick(midKey)">
        <span class="ic" :style="midStyle"><el-icon><component :is="MODES[midKey].icon" /></el-icon></span>
      </button>
      <button class="sq" :title="MODES[botKey].name" @click="pick(botKey)">
        <span class="ic" :style="botStyle"><el-icon><component :is="MODES[botKey].icon" /></el-icon></span>
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Moon, Monitor, Sunny } from '@element-plus/icons-vue'
import { useThemeStore } from '@/stores/theme'

const theme = useThemeStore()

const MODES = {
  light: { name: '日间', icon: Sunny },
  dark: { name: '夜间', icon: Moon },
  system: { name: '跟随系统', icon: Monitor },
}
const KEYS = Object.keys(MODES)
const CELL = 37   // 格高 36 + 1px 分隔线
const GROW_MS = 300   // 展开时长
const SHRINK_MS = 580 // 收缩时长：一次连续动画滚过两格，匀速无衔接点

const open = ref(false)
const busy = ref(false)
const boxH = ref('')
const tmTrans = ref('height .3s linear')
const midKey = ref('light')
const botKey = ref('dark')
const topStyle = ref({})
const midStyle = ref({})
const botStyle = ref({})

const curKey = computed(() => theme.mode)
const otherKeys = () => KEYS.filter((k) => k !== curKey.value)

// 顶格始终显示当前模式，下面两格轮换展示其余两个
watch(curKey, () => {
  const [a, b] = otherKeys()
  midKey.value = a
  botKey.value = b
})

function onTop() {
  if (busy.value) return
  if (open.value) { shrink(null); return }
  // 展开：重置上一次收起遗留的飞入样式，再向下生长
  midStyle.value = {}
  botStyle.value = {}
  topStyle.value = {}
  tmTrans.value = `height ${GROW_MS}ms linear`
  boxH.value = '110px'
  open.value = true
}

// 收起（一次连续动画）：图标流整体向上滚两格，容器底边同步升两格。
// selectedKey 为 null = 点顶格取消，图标内容不变。
// 落位后：先 setMode（顶格内容换成新图标，此刻它仍透明），下一帧顶格淡入，
// 飞入层保持覆盖到新图标显形才清理——全程按钮区域不会出现空档或跳变。
function shrink(selectedKey) {
  busy.value = true
  open.value = false
  tmTrans.value = `height ${SHRINK_MS}ms linear`

  if (selectedKey && selectedKey === botKey.value) {
    // 选的是第三格：让它顶替第二格的位置参与上滚（交换内容，瞬时无感）
    const t = midKey.value
    midKey.value = botKey.value
    botKey.value = t
  }

  // 选中图标（中格）滚入顶格；旧顶格图标淡出让位
  midStyle.value = { transform: `translateY(-${CELL}px)`, zIndex: 2 }
  topStyle.value = { opacity: 0 }
  // 落选图标跟在后面上滑，行进到后半程才消失
  botStyle.value = {
    transform: `translateY(-${CELL}px)`,
    opacity: 0,
    transition: `transform ${SHRINK_MS}ms linear, opacity ${Math.round(SHRINK_MS / 2)}ms linear ${Math.round(SHRINK_MS / 2)}ms`,
  }
  boxH.value = '36px'

  setTimeout(() => {
    if (selectedKey) theme.setMode(selectedKey)
    requestAnimationFrame(() => {
      topStyle.value = {} // 顶格新图标淡入（.ic 自带 opacity 过渡）
      setTimeout(() => {
        midStyle.value = {}
        botStyle.value = {}
        busy.value = false
      }, 250)
    })
  }, SHRINK_MS + 30)
}

// 点选模式
function pick(key) {
  if (busy.value || !open.value) return
  shrink(key)
}

// 展开时点击组件以外的地方，视为取消收起
function onDocClick(e) {
  if (!open.value || busy.value) return
  shrink(null)
}
onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<style scoped>
.tm-wrap {
  position: relative;
  width: 38px;
  height: 36px;
}

/* 长条悬浮生长，不撑开 header：绝对定位 + 固定起点，高度由脚本驱动 */
.tm {
  position: absolute;
  top: 0;
  left: 0;
  width: 38px;
  height: 36px;
  overflow: hidden;
  background: var(--app-bg-surface);
  border: 1px solid var(--app-border);
  border-radius: 9px;
  z-index: 20;
}

.tm .sq {
  width: 100%;
  height: 36px;
  border: none;
  background: transparent;
  color: var(--app-text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  position: relative;
}

.tm .sq + .sq::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 1px;
  background: var(--app-border);
}

.tm .sq:hover {
  background: var(--app-fill);
}

.tm .sq .ic {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: transform .58s linear, opacity .25s linear;
}

.tm .cur-sq {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.tm .cur-sq:hover {
  background: var(--el-color-primary-light-9);
}
</style>
