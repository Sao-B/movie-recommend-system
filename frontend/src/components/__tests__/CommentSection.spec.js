/**
 * 详情页影评区的交互约定 —— 真实渲染 + 真实 DOM 断言。
 *
 * 这里替代的是原先 `tests/test_comment_ui_contract.py` 的做法：那份文件读 .vue 源码文本、
 * 用 split()/assertIn 断言"某个字符串出现过"。那种写法有两个毛病：
 * 改个变量名、调个属性顺序就红（假红），而行为真被改坏时只要换个写法就绿（假绿）。
 * 下面这些断言打在渲染结果与组件 props 上，重构不会误伤，行为变了就会红。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus, { ElRate } from 'element-plus'
import CommentSection from '@/components/CommentSection.vue'
import CommentItem from '@/components/CommentItem.vue'

// 接口与路由都替身掉，只留组件自身的行为
vi.mock('@/api/comments', () => ({
  apiComments: { list: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api/ratings', () => ({
  apiRatings: { submit: vi.fn(), remove: vi.fn() },
}))
vi.mock('vue-router', () => ({
  onBeforeRouteLeave: vi.fn(),
  useRoute: () => ({ params: {}, query: {} }),
  useRouter: () => ({ push: vi.fn() }),
  RouterLink: { template: '<a><slot /></a>' },
}))

import { apiComments } from '@/api/comments'

const MY_ID = 7
const MY_COMMENT = {
  id: 1, user_id: MY_ID, username: 'me', content: '我的影评正文',
  user_rating: 4.5, created_at: '2026-09-01 10:00:00',
}
const OTHER_COMMENT = {
  id: 2, user_id: 9, username: 'other', content: '别人的影评正文',
  user_rating: 5, created_at: '2026-09-02 10:00:00',
}

async function mountSection({ myComment = MY_COMMENT, comments = [MY_COMMENT, OTHER_COMMENT] } = {}) {
  apiComments.list.mockResolvedValue({ comments, my_comment: myComment, total: comments.length })
  const wrapper = mount(CommentSection, {
    props: { movieId: 318, movieTitle: '肖申克的救赎', myRating: 4.5, savedRating: 4.5 },
    global: {
      plugins: [ElementPlus],
      stubs: { RouterLink: { template: '<a><slot /></a>' } },
    },
  })
  await flushPromises()
  return wrapper
}

// 第 index 张卡片上的「编辑」按钮 —— 互斥协议的可见结果就看它
function editButtonOf(wrapper, index) {
  return wrapper
    .findAllComponents(CommentItem)[index]
    .findAll('button')
    .find((button) => button.text().includes('编辑'))
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  localStorage.setItem('token', 'test-token')
  localStorage.setItem('user', JSON.stringify({ id: MY_ID, username: 'me', role: 'user' }))
  vi.clearAllMocks()
})

describe('详情页影评区的交互约定', () => {
  it('评分控件不开 clearable —— 点同一颗星不会把评分归零', async () => {
    // 还没发表过，录入区（星星那一行）才在
    const wrapper = await mountSection({ myComment: null, comments: [OTHER_COMMENT] })

    const rate = wrapper.findComponent(ElRate)
    expect(rate.exists()).toBe(true)
    expect(rate.props('clearable')).toBe(false)
  })

  it('星星旁边没有独立的删除入口 —— 撤销评分只走影评卡片的确认框', async () => {
    const wrapper = await mountSection({ myComment: null, comments: [OTHER_COMMENT] })

    const rateRow = wrapper.find('.rate-row')
    expect(rateRow.exists()).toBe(true)
    expect(rateRow.findAll('button')).toHaveLength(0)
    expect(rateRow.find('[class*="delete"]').exists()).toBe(false)
  })

  it('录入区的显示名左侧亮出当前用户头像 —— 没头像时回落显示名首字符', async () => {
    const wrapper = await mountSection({ myComment: null, comments: [OTHER_COMMENT] })

    const rateRow = wrapper.find('.rate-row')
    expect(rateRow.find('.user-avatar').exists()).toBe(true)
    // localStorage 里的 user 没有 avatar_url → 回落成显示名首字符（username 'me' → 'M'）
    expect(rateRow.find('.user-avatar img').exists()).toBe(false)
    expect(rateRow.find('.user-avatar').text()).toBe('M')
  })

  it('自己的影评置顶展示，数据来自接口的 my_comment', async () => {
    const wrapper = await mountSection()

    const first = wrapper.findAllComponents(CommentItem)[0]
    expect(first.props('pinned')).toBe(true)
    expect(first.props('comment').id).toBe(MY_COMMENT.id)
    expect(wrapper.text()).toContain(MY_COMMENT.content)
  })

  it('每张影评卡片都带作者头像 —— 有图用图，没图回落显示名首字符', async () => {
    const withAvatar = {
      ...OTHER_COMMENT,
      avatar_url: '/api/profile/avatar/avatar_9_0123456789abcdef0123456789abcdef.png',
    }
    const wrapper = await mountSection({ comments: [MY_COMMENT, withAvatar] })

    const cards = wrapper.findAllComponents(CommentItem)
    expect(cards.length).toBeGreaterThan(1)
    for (const card of cards) {
      expect(card.find('.user-avatar').exists()).toBe(true)
    }

    // 接口给了 avatar_url → 渲染成图片
    const withImg = cards.find((card) => card.props('comment').id === withAvatar.id)
    expect(withImg.find('img').attributes('src')).toBe(withAvatar.avatar_url)

    // 没给 avatar_url → 回落成显示名首字符（username 'me' → 'M'）
    const noImg = cards.find((card) => card.props('comment').id === MY_COMMENT.id)
    expect(noImg.find('img').exists()).toBe(false)
    expect(noImg.find('.user-avatar').text()).toBe('M')
  })

  it('全页同时只允许一条影评处于编辑态，其余卡片的编辑按钮被禁用', async () => {
    const wrapper = await mountSection()

    // 时间线里自己那条（索引 1）与置顶那条（索引 0）是同一份数据
    await editButtonOf(wrapper, 1).trigger('click')
    await flushPromises()

    const items = wrapper.findAllComponents(CommentItem)
    expect(items[1].props('editing')).toBe(true)
    expect(items[0].props('editing')).toBe(false)
    expect(items[0].props('editLocked')).toBe(true)
    // 锁住的卡片，编辑按钮是真的点不动，不只是 prop 上写着
    expect(editButtonOf(wrapper, 0).attributes('disabled')).toBeDefined()
  })
})
