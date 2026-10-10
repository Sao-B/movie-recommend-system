/**
 * 个人主页换头像的入口与落库：**点自己的头像 → 选内置 SVG 头像 → 立刻保存**。
 *
 * 上传那条路已经撤掉，所以这里也顺带锁住「面板里不是文件选择器」这一点。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'

vi.mock('@/api/profile', () => ({
  getUserInfo: vi.fn(),
  updateUserInfo: vi.fn(),
  getPresetAvatars: vi.fn(),
  getUserStats: vi.fn(),
  getUserComments: vi.fn(),
}))
vi.mock('@/api/comments', () => ({ apiComments: { update: vi.fn(), delete: vi.fn() } }))
vi.mock('@/api/ratings', () => ({ apiRatings: { submit: vi.fn() } }))
vi.mock('@/api/dislikes', () => ({
  apiDislikes: { add: vi.fn(), remove: vi.fn(), list: vi.fn() },
}))

import { getUserInfo, updateUserInfo, getPresetAvatars, getUserStats, getUserComments } from '@/api/profile'
import { apiDislikes } from '@/api/dislikes'
import { ElMessageBox } from 'element-plus'
import ProfileView from '@/views/ProfileView.vue'

const PRESETS = [
  { id: 'film', name: '胶片', url: '/api/profile/avatars/film.svg' },
  { id: 'cat', name: '猫咪', url: '/api/profile/avatars/cat.svg' },
]
const USER = {
  id: 7, username: 'movie_fan', nickname: '影迷小王',
  avatar_url: '', bio: '', created_at: '2026-09-18 10:00:00',
}

const DISLIKE_ITEM = { movie_id: 318, title: 'Shawshank', year: 1994, genres: ['Drama'] }

async function mountProfile(user = {}) {
  getUserInfo.mockResolvedValue({ ...USER, ...user })
  getPresetAvatars.mockResolvedValue(PRESETS)
  getUserStats.mockResolvedValue({
    total_ratings: 0, avg_rating: 0, total_comments: 0,
    rating_distribution: {}, top_genres: [], top_directors: [],
  })
  getUserComments.mockResolvedValue({ items: [], total: 0 })

  const wrapper = mount(ProfileView, {
    global: {
      plugins: [ElementPlus],
      stubs: {
        RouterLink: { template: '<a><slot /></a>' },
        teleport: true,
        loading: { template: '<i class="loading-stub" />' },
      },
    },
  })
  await flushPromises()
  return wrapper
}

async function openPicker(wrapper) {
  await wrapper.find('.avatar-clickable').trigger('click')
  await flushPromises()
  return wrapper.findAll('.preset-item')
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  localStorage.setItem('token', 'test-token')
  localStorage.setItem('user', JSON.stringify({ id: USER.id, username: USER.username }))
  vi.clearAllMocks()
  apiDislikes.list.mockResolvedValue({
    count: 1, movie_ids: [DISLIKE_ITEM.movie_id], items: [DISLIKE_ITEM],
  })
})

describe('个人主页头像选择', () => {
  it('点头像打开选择面板，并去取内置头像清单', async () => {
    const wrapper = await mountProfile()
    expect(getPresetAvatars).not.toHaveBeenCalled()   // 没点之前不请求

    const items = await openPicker(wrapper)
    expect(getPresetAvatars).toHaveBeenCalledTimes(1)
    expect(items).toHaveLength(PRESETS.length)
    // 面板里是内置头像，不是上传用的文件选择器
    expect(wrapper.find('input[type="file"]').exists()).toBe(false)
  })

  it('选中一个内置头像 → 用它的地址落库，顶栏头像同步', async () => {
    updateUserInfo.mockResolvedValue({ ...USER, avatar_url: PRESETS[1].url })
    const wrapper = await mountProfile()

    await openPicker(wrapper)
    await wrapper.findAll('.preset-item')[1].trigger('click')
    await flushPromises()

    expect(updateUserInfo).toHaveBeenCalledWith({ avatar_url: PRESETS[1].url })
    expect(JSON.parse(localStorage.getItem('user')).avatar_url).toBe(PRESETS[1].url)
  })

  it('点「恢复默认」→ 用 null 落库（后端存 NULL，界面回落显示名首字符）', async () => {
    updateUserInfo.mockResolvedValue({ ...USER, avatar_url: null })
    const wrapper = await mountProfile({ avatar_url: PRESETS[0].url })

    await openPicker(wrapper)
    const reset = wrapper.findAll('button').find((button) => button.text().includes('恢复默认'))
    await reset.trigger('click')
    await flushPromises()

    expect(updateUserInfo).toHaveBeenCalledWith({ avatar_url: null })
  })
})

describe('个人主页偏好设置卡（类型 + 不感兴趣）', () => {
  it('进入页面就拉取不感兴趣列表，并在偏好设置卡里逐条展示', async () => {
    const wrapper = await mountProfile()

    expect(apiDislikes.list).toHaveBeenCalledTimes(1)
    const items = wrapper.findAll('.dislike-item')
    expect(items).toHaveLength(1)
    expect(items[0].text()).toContain('Shawshank')
    expect(items[0].text()).toContain('Drama')
  })

  it('点「取消标记」→ 先弹二次确认，确认后 DELETE 落库、该条立即从列表消失', async () => {
    apiDislikes.remove.mockResolvedValue({ deleted: true })
    const confirmSpy = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm')
    const wrapper = await mountProfile()

    await wrapper.findAll('.dislike-item')[0].find('button').trigger('click')
    await flushPromises()

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(apiDislikes.remove).toHaveBeenCalledWith(318)
    expect(wrapper.findAll('.dislike-item')).toHaveLength(0)
  })

  it('确认框里点了「再想想」→ 不调 DELETE，列表原样保留', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    const wrapper = await mountProfile()

    await wrapper.findAll('.dislike-item')[0].find('button').trigger('click')
    await flushPromises()

    expect(apiDislikes.remove).not.toHaveBeenCalled()
    expect(wrapper.findAll('.dislike-item')).toHaveLength(1)
  })

  it('一条都没标记过时显示空状态文案', async () => {
    apiDislikes.list.mockResolvedValue({ count: 0, movie_ids: [], items: [] })
    const wrapper = await mountProfile()

    expect(wrapper.findAll('.dislike-item')).toHaveLength(0)
    expect(wrapper.find('.preferences-card').text()).toContain('没有标记过不感兴趣的电影')
  })
})
