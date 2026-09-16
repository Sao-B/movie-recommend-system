<template>
  <div class="rating-stats" v-loading="loading">
    <h4 class="stats-title">评分统计</h4>

    <div v-if="!loading && !count" class="stats-empty">
      暂无评分，成为第一个打分的人
    </div>

    <div v-else-if="count" class="stats-body">
      <!-- 左：平均分 + 星条 + 评分人数 -->
      <div class="stats-left">
        <div class="stats-avg">
          <span class="avg-num">{{ averageText }}</span>
          <span class="avg-unit">/ 5</span>
        </div>
        <div class="stars">
          <span class="stars-bg">★★★★★</span>
          <span class="stars-fg" :style="{ width: starWidth }">★★★★★</span>
        </div>
        <!-- 评分人数 = MovieLens 评分用户数 + 本站用户评分数（后端已合并，不区分来源） -->
        <div class="rate-count">评分人数 {{ countText }}</div>
      </div>

      <!-- 右：五档分布 -->
      <div class="stats-right">
        <div v-for="row in distribution" :key="row.stars" class="bar-row">
          <span class="bar-lbl">{{ row.stars }} 星</span>
          <span class="bar-track">
            <span class="bar-fill" :style="{ width: row.percent + '%' }" />
          </span>
          <span class="bar-pct">{{ row.percent }}%</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  count:        { type: Number,  default: 0 },
  average:      { type: Number,  default: null },
  distribution: { type: Array,   default: () => [] },
  loading:      { type: Boolean, default: false }
})

const averageText = computed(() =>
  props.average == null ? '—' : Number(props.average).toFixed(1)
)

// 评分人数：MovieLens 那边的量级上万，加千分位读起来才不吃力
const countText = computed(() =>
  Number(props.count || 0).toLocaleString('zh-CN')
)

const starWidth = computed(() => {
  if (props.average == null) return '0%'
  const pct = Math.max(0, Math.min(100, (Number(props.average) / 5) * 100))
  return pct + '%'
})
</script>

<style scoped>
.rating-stats {
  min-height: 60px;
}

.stats-title {
  font-size: 16px;
  margin: 0 0 12px;
  color: var(--app-text);
}

.stats-body {
  display: flex;
  gap: 32px;
  align-items: flex-start;
  padding: 16px 18px;
  border: 1px solid var(--app-border);
  border-radius: 6px;
  background: var(--app-fill-light);
}

/* 左侧平均分 */
.stats-left {
  flex: 0 0 158px;
}

.stats-avg {
  display: flex;
  align-items: baseline;
  gap: 4px;
}

.avg-num {
  font-size: 34px;
  font-weight: 600;
  line-height: 1;
  color: #f7ba2a;
}

.avg-unit {
  font-size: 13px;
  color: var(--app-text-muted);
}

.stars {
  position: relative;
  display: inline-block;
  margin-top: 10px;
  font-size: 25px;
  line-height: 1;
  letter-spacing: 3px;
  white-space: nowrap;
}

.stars-bg {
  color: var(--app-text-muted);
}

.stars-fg {
  position: absolute;
  left: 0;
  top: 0;
  color: #f7ba2a;
  overflow: hidden;
  white-space: nowrap;
}

/* 评分人数：压在星条下方 */
.rate-count {
  margin-top: 8px;
  font-size: 13px;
  color: var(--app-text-muted);
}

/* 右侧分布条 */
.stats-right {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 2px;
}

.bar-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.bar-lbl {
  flex: 0 0 38px;
  font-size: 12px;
  color: var(--app-text-muted);
}

.bar-track {
  flex: 1;
  min-width: 0;
  height: 9px;
  border-radius: 5px;
  background: var(--app-fill);
  overflow: hidden;
}

.bar-fill {
  display: block;
  height: 100%;
  border-radius: 5px;
  background: #f7ba2a;
  transition: width 0.3s;
}

.bar-pct {
  flex: 0 0 46px;
  text-align: right;
  font-size: 12px;
  color: var(--app-text-secondary);
}

.stats-empty {
  padding: 16px 18px;
  border: 1px dashed var(--app-border-light);
  border-radius: 6px;
  background: var(--app-fill-light);
  text-align: center;
  color: var(--app-text-muted);
  font-size: 13px;
}

@media (max-width: 768px) {
  .stats-body {
    flex-direction: column;
    gap: 16px;
  }
  .stats-left {
    flex: none;
  }
}
</style>
