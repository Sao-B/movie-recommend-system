/**
 * 后台用户管理页：分页 / 排序 / 删除后回退页码。
 *
 * 这一页是 `composables/useTableQuery.js` 的三个调用方之一。抽那个 composable 的收益是
 * 「改一处、三处全对」，代价是多一层间接 —— 所以这里把那两条**易错规则**用行为钉住：
 *   ① 点「搜索」无论当前在第几页都回到第 1 页；
 *   ② 表头排序要把 `ascending|descending` 映射成 `asc|desc`，取消排序时不再带参数；
 *   ③ 删掉末页最后一条后页码要回退，不能停在空白页。
 * 在此之前这三条只有「构建通过」这一层保障（grep 过：没有任何测试碰它们）。
 */
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus, { ElMessage, ElMessageBox, ElPagination, ElTable } from 'element-plus'
import AdminUsers from '@/views/admin/AdminUsers.vue'

vi.mock('@/api/admin', () => ({
  apiAdmin: {
    listUsers: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    deleteUser: vi.fn(),
  },
}))

import { apiAdmin } from '@/api/admin'

const USERS = [
  { id: 1, username: 'alice', role: 'user', created_at: '2026-09-01 10:00:00' },
  { id: 2, username: 'bob', role: 'admin', created_at: '2026-09-02 10:00:00' },
]

// el-table 在 jsdom 里依赖 ResizeObserver
beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
})

async function mountPage({ total = 21 } = {}) {
  apiAdmin.listUsers.mockResolvedValue({ users: USERS, total })
  const wrapper = mount(AdminUsers, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

// 最近一次列表请求带的参数 —— 就是被测的"发出去的东西"
const lastParams = () => apiAdmin.listUsers.mock.calls.at(-1)[0]

async function goToPage(wrapper, page) {
  const pager = wrapper.findComponent(ElPagination)
  pager.vm.$emit('update:current-page', page)
  pager.vm.$emit('current-change', page)
  await flushPromises()
}

const findButton = (wrapper, label) =>
  wrapper.findAll('button').find((button) => button.text() === label)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.spyOn(ElMessage, 'success').mockImplementation(() => {})
})

describe('后台用户管理页', () => {
  it('点「搜索」时，无论当前在第几页都回到第 1 页', async () => {
    const wrapper = await mountPage()

    await goToPage(wrapper, 2)
    expect(lastParams().page).toBe(2)

    await findButton(wrapper, '搜索').trigger('click')
    await flushPromises()

    expect(lastParams().page).toBe(1)
  })

  it('表头排序映射成 sort/order；取消排序时不再带这两个参数', async () => {
    const wrapper = await mountPage()
    const table = wrapper.findComponent(ElTable)

    table.vm.$emit('sort-change', { prop: 'username', order: 'ascending' })
    await flushPromises()
    expect(lastParams()).toMatchObject({ sort: 'username', order: 'asc' })

    table.vm.$emit('sort-change', { prop: 'created_at', order: 'descending' })
    await flushPromises()
    expect(lastParams()).toMatchObject({ sort: 'created_at', order: 'desc' })

    table.vm.$emit('sort-change', { prop: 'username', order: null })
    await flushPromises()
    expect(lastParams()).not.toHaveProperty('sort')
    expect(lastParams()).not.toHaveProperty('order')
  })

  it('删掉末页最后一条后页码回退，不会停在空白页', async () => {
    const wrapper = await mountPage({ total: 21 })
    await goToPage(wrapper, 2)

    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm')
    apiAdmin.deleteUser.mockResolvedValue({})

    const deleteButton = findButton(wrapper, '删除')
    expect(deleteButton, '表格里应渲染出「删除」按钮').toBeTruthy()

    await deleteButton.trigger('click')
    await flushPromises()

    expect(apiAdmin.deleteUser).toHaveBeenCalledWith(1)
    // 第 2 页删完之后 total=21 只剩 20 条 → 应回到第 1 页
    expect(lastParams().page).toBe(1)
  })
})
