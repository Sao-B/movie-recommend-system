import { ref } from 'vue'

/**
 * 后台表格页共用的「分页 + 排序 + 加载」骨架。
 *
 * 各页的接口参数名不一样（`per_page` / `q` / `user_id` …），所以由调用方传一个
 * `loader`，它拿当前状态去调自己的接口，并回传 `{ rows, total }`。
 * 这里只管三件重复的事：加载中标志、排序切换、删掉末页最后一条后的页码回退。
 */
export function useTableQuery(loader, { pageSize: initialPageSize = 20 } = {}) {
  const loading = ref(false)
  const rows = ref([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(initialPageSize)
  const sortProp = ref('')
  const sortOrder = ref('')

  async function load() {
    loading.value = true
    try {
      const { rows: list, total: count } = await loader({
        page: page.value,
        pageSize: pageSize.value,
        sortProp: sortProp.value,
        sortOrder: sortOrder.value,
      })
      rows.value = list
      total.value = count
    } finally {
      loading.value = false
    }
  }

  function search() {
    page.value = 1
    return load()
  }

  // 表头点击切换排序；order 为空表示取消排序，回到默认
  function handleSortChange({ prop, order }) {
    if (order) {
      sortProp.value = prop
      sortOrder.value = order === 'ascending' ? 'asc' : 'desc'
    } else {
      sortProp.value = ''
      sortOrder.value = ''
    }
    search()
  }

  // 删掉当前页最后一条时把页码退回去，避免停在空白页
  function reloadAfterDelete() {
    const lastPage = Math.max(1, Math.ceil((total.value - 1) / pageSize.value))
    if (page.value > lastPage) page.value = lastPage
    return load()
  }

  return {
    loading,
    rows,
    total,
    page,
    pageSize,
    sortProp,
    sortOrder,
    load,
    search,
    handleSortChange,
    reloadAfterDelete,
  }
}
