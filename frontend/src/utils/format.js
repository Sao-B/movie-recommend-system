// 全站共用的时间格式化

// 后端 DATETIME 里存的是 UTC，序列化出来的 naive 字符串（无时区后缀）要按 UTC 解析再转本地，
// 否则会整体偏移一个时区。
function parseUtc(value) {
  const hasZone = /([zZ]|[+-]\d{2}:?\d{2})$/.test(value)
  const iso = hasZone ? value : `${String(value).replace(' ', 'T')}Z`
  return new Date(iso)
}

export function formatDateTime(value) {
  if (!value) return '-'
  const date = parseUtc(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', { hour12: false })
}

// 相对时间：「刚刚 / n 分钟前 / n 小时前 / n 天前」，超过一周显示日期
export function formatRelative(value) {
  if (!value) return ''
  const date = parseUtc(value)
  if (Number.isNaN(date.getTime())) return String(value)

  const diff = Date.now() - date.getTime()
  if (diff < 60000) return '刚刚'
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`
  if (diff < 604800000) return `${Math.floor(diff / 86400000)} 天前`
  return date.toLocaleDateString('zh-CN')
}
