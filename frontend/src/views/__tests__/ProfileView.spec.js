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

import { getUserInfo, updateUserInfo, getPresetAvatars, getUserStats, getUserComments } from '@/api/profile'
import ProfileView from '@/views/ProfileView.vue'

const PRESETS = [
  { id: 'film', name: '胶片', url: '/api/profile/avatars/film.svg' },
  { id: 'cat', name: '猫咪', url: '/api/profile/avatars/cat.svg' },
]
const USER = {
  id: 7, username: 'movie_fan', nickname: '影迷小王',
  avatar_url: '', bio: '', created_at: '2026-09-18 10:00:00',
}

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
