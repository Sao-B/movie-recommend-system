<template>
  <div class="tm-wrap" @click.stop>
    <div class="tm" :style="{ height: boxH }">
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
const CELL = 37 // 格高 36 + 1px 分隔线
const STEP = 290 // 每级收缩时长：与 CSS transition 一致，两段严丝合缝保持匀速

const open = ref(false)
const busy = ref(false)
const boxH = ref('')
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
  if (open.value) { collapse(); return }
  open.value = true
  boxH.value = '110px'
}

// 点顶格（当前模式）= 取消：逐级收起，不换模式
function collapse() {
  busy.value = true
  botStyle.value = { transform: `translateY(-${CELL}px)`, opacity: 0 }
  boxH.value = '74px'
  setTimeout(() => {
    boxH.value = '36px'
    midStyle.value = { transform: `translateY(-${CELL}px)`, opacity: 0 }
    setTimeout(() => {
      boxH.value = ''
      midStyle.value = {}
      botStyle.value = {}
      open.value = false
      busy.value = false
    }, STEP)
  }, STEP)
}

// 点选模式：甩棍式逐级收起——内容每步向上匀速滚一格、底边同步上升一格。
// 收尾用交叉淡入：先 setMode 让顶格内容换成新图标（此刻透明不可见），
// 下一帧顶格淡入、飞入层保持覆盖，新图标显形后才清理，避免落位闪烁。
function pick(key) {
  if (busy.value || !open.value) return
  if (key === curKey.value) { collapse(); return }
  busy.value = true
  const replacing = key === botKey.value

  if (replacing) {
    // 第三格图标向上取代第二格
    botStyle.value = { transform: `translateY(-${CELL}px)` }
    setTimeout(() => {
      botStyle.value = { transform: `translateY(-${CELL * 2}px)`, zIndex: 2 }
      midStyle.value = { opacity: 0 }
    }, STEP)
  } else {
    // 第二格被选中：第三格向上滑走但不取代
    botStyle.value = { transform: `translateY(-${CELL}px)`, opacity: 0 }
  }
  boxH.value = '74px'

  setTimeout(() => {
    boxH.value = '36px'
    if (replacing) {
      // 停在顶格位置（-74），不是 -111：飞入图标要落位覆盖，不能滚出顶部
      botStyle.value = { transform: `translateY(-${CELL * 2}px)`, zIndex: 2 }
    } else {
      midStyle.value = { transform: `translateY(-${CELL}px)`, zIndex: 2 }
    }
    topStyle.value = { opacity: 0 }
    setTimeout(() => {
      theme.setMode(key)
      requestAnimationFrame(() => {
        topStyle.value = {}
        setTimeout(() => {
          midStyle.value = {}
          botStyle.value = {}
          boxH.value = ''
          open.value = false
          busy.value = false
        }, 200)
      })
    }, STEP)
  }, STEP)
}

// 展开时点击组件以外的地方，视为取消收起
function onDocClick(e) {
  if (!open.value || busy.value) return
  collapse()
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
  /* linear：两段收缩严丝合缝，整条匀速滑动，没有先快后慢的顿挫 */
  transition: height .29s linear;
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
  transition: transform .29s linear, opacity .18s linear;
}

.tm .cur-sq {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.tm .cur-sq:hover {
  background: var(--el-color-primary-light-9);
}
</style>
