/**
 * 推荐页三条链路的独立网址：/recommendations/als | /itemcf | /fusion
 *
 * 模型不再存在组件内部状态里，而是由 `route.params.model` 派生——
 * 于是"点按钮"必须是路由跳转、而"网址变了"必须重新拉数。
 *
 * 顺带锁住"不感兴趣"的入库路径：确认动作已经挪到卡片菜单里（那段见 MovieCard.spec.js），
 * 推荐页收到 dislike 就该直接落库 + 把卡片从当前列表摘掉，不再有浮窗二次确认。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElRadioGroup } from 'element-plus'

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }))

vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const route = reactive({ params: { model: 'als' } })
  return { __route: route, useRoute: () => route, useRouter: () => ({ push: pushMock }) }
})
vi.mock('@/api/recommendations', () => ({ apiRecommendations: { get: vi.fn() } }))
vi.mock('@/api/dislikes', () => ({ apiDislikes: { add: vi.fn(), remove: vi.fn(), list: vi.fn() } }))

import { __route } from 'vue-router'
import { apiRecommendations } from '@/api/recommendations'
import { apiDislikes } from '@/api/dislikes'
import MovieCard from '@/components/MovieCard.vue'
import RecommendationsView from '@/views/RecommendationsView.vue'

const ITEM = {
  movie: { movie_id: 318, title: 'The Shawshank Redemption', year: 1994, genres: ['Drama'] },
  predicted_rating: 4.9,
}

async function mountView(items = []) {
  apiRecommendations.get.mockResolvedValue({ data: items })
  const wrapper = mount(RecommendationsView, {
    global: {
      plugins: [ElementPlus],
      stubs: {
        RouterLink: { template: '<a><slot /></a>' },
        loading: { template: '<i />' },
        // 不 stub teleport：卡片菜单靠 teleport 到 body，stub 掉会让 ElTooltip 在 jsdom 里反复更新报错
      },
    },
  })
  await flushPromises()
  return wrapper
}

// 卡片菜单 teleport 到 body，用例之间清掉残留
afterEach(() => { document.body.innerHTML = '' })

describe('RecommendationsView 的模型由网址决定', () => {
  beforeEach(() => {
    pushMock.mockClear()
    apiRecommendations.get.mockReset()
    apiDislikes.add.mockReset()
    __route.params.model = 'als'
  })

  it('三个按钮都在，文案与网址一一对应', async () => {
    const wrapper = await mountView()
    const labels = wrapper.findAll('.el-radio-button__inner').map((n) => n.text())
    expect(labels).toEqual(['ALS 协同过滤', 'ItemCF 相似度', '混合融合'])
  })

  it('直接打开 /recommendations/itemcf 就按 itemcf 取数', async () => {
    __route.params.model = 'itemcf'
    await mountView()
    expect(apiRecommendations.get).toHaveBeenCalledWith('itemcf')
  })

  it('点「混合融合」走路由跳转，而不是只改本地状态', async () => {
    const wrapper = await mountView()
    apiRecommendations.get.mockClear()
    wrapper.findComponent(ElRadioGroup).vm.$emit('change', 'fusion')
    await flushPromises()
    expect(pushMock).toHaveBeenCalledWith({
      name: 'recommendation-model',
      params: { model: 'fusion' },
    })
  })

  it('网址切到 fusion 后重新拉一次 fusion 的推荐', async () => {
    await mountView()
    apiRecommendations.get.mockClear()
    __route.params.model = 'fusion'
    await flushPromises()
    expect(apiRecommendations.get).toHaveBeenCalledWith('fusion')
  })

  it('网址里是非法模型时回落 ALS，不会拿脏值去打接口', async () => {
    __route.params.model = 'nope'
    const wrapper = await mountView()
    expect(apiRecommendations.get).toHaveBeenCalledWith('als')
    expect(wrapper.text()).toContain('基于 ALS 协同过滤')
  })

  it('卡片确认过的"不感兴趣"直接入库，并从当前列表摘掉', async () => {
    apiDislikes.add.mockResolvedValue({})
    const wrapper = await mountView([ITEM])
    wrapper.findComponent(MovieCard).vm.$emit('dislike', ITEM)
    await flushPromises()
    expect(apiDislikes.add).toHaveBeenCalledWith(318)
    expect(wrapper.text()).not.toContain('The Shawshank Redemption')
  })
})
