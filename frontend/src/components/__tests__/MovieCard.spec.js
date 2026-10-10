/**
 * 推荐卡片"⋯"菜单里的「不感兴趣」：**两段式确认**。
 *
 * 第一下只把菜单项点红成"确定吗？"（菜单不能自动收，不然第二下没地方点）；
 * 第二下才 emit dislike，由推荐页落库。菜单收起（点了别处）要复位回起点的样子。
 *
 * 注意两点：
 * ① 菜单默认 teleport 到 body，这里**不 stub teleport**——stub 之后 ElTooltip 的定位逻辑
 *    会在 jsdom 里反复触发更新并抛 "Maximum recursive updates"。菜单项改为 document 查询。
 * ② 走真实点击（点"⋯"开菜单、再点菜单项），而不是直接 $emit('command')——后者绕过了
 *    ElDropdown 自己的收起逻辑，测不出 hide-on-click=false 到底有没有用。
 */
import { afterEach, describe, expect, it } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import ElementPlus, { ElDropdown } from 'element-plus'
import MovieCard from '@/components/MovieCard.vue'

const MOVIE = { movie_id: 1, title: 'Sunset Blvd', year: 1950, genres: ['Drama', 'Film-Noir'] }

let wrapper = null

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

function mountCard(props = { dismissible: true }) {
  wrapper = mount(MovieCard, {
    props: { movie: MOVIE, ...props },
    global: {
      plugins: [ElementPlus],
      stubs: { RouterLink: { template: '<a><slot /></a>' } },
    },
  })
  return wrapper
}

const menuItem = () => document.querySelector('.dislike-item')
const menuText = () => menuItem()?.textContent.trim()

// 点"⋯"打开菜单
async function openMenu() {
  await wrapper.find('.more-btn').trigger('click')
  await nextTick()
}

// 点菜单里的「不感兴趣」/「确定吗？」
async function clickMenuItem() {
  menuItem().dispatchEvent(new MouseEvent('click', { bubbles: true }))
  await nextTick()
  await nextTick()
}

describe('MovieCard 的「不感兴趣」两段式确认', () => {
  it('菜单项常态是红色文案「不感兴趣」', () => {
    mountCard()
    expect(menuItem()).not.toBeNull()
    expect(menuText()).toBe('不感兴趣')
    expect(menuItem().classList.contains('is-armed')).toBe(false)
  })

  it('菜单点击不自动收起，否则第二下没地方点', () => {
    mountCard()
    expect(wrapper.findComponent(ElDropdown).props('hideOnClick')).toBe(false)
  })

  it('红色样式挂在 popper-class 上（菜单 teleport 到 body，scoped 样式追不进去）', () => {
    mountCard()
    expect(wrapper.findComponent(ElDropdown).props('popperClass')).toBe('movie-dislike-menu')
  })

  it('第一下只把按钮点红成「确定吗？」，不 emit', async () => {
    mountCard()
    await openMenu()
    await clickMenuItem()
    expect(menuText()).toBe('确定吗？')
    expect(menuItem().classList.contains('is-armed')).toBe(true)
    expect(wrapper.emitted('dislike')).toBeUndefined()
  })

  it('第二下才 emit dislike', async () => {
    mountCard()
    await openMenu()
    await clickMenuItem()
    expect(wrapper.emitted('dislike')).toBeUndefined()
    await clickMenuItem()
    // 卡片只报"我不感兴趣了"，具体是哪一部由父组件的 v-for 决定，所以不带参数
    expect(wrapper.emitted('dislike')).toHaveLength(1)
  })

  it('菜单收起后复位，下次要重新点两下', async () => {
    mountCard()
    const dropdown = wrapper.findComponent(ElDropdown)
    await openMenu()
    await clickMenuItem()
    expect(menuText()).toBe('确定吗？')

    dropdown.vm.$emit('visible-change', false) // 点了别处 → 菜单收起
    await nextTick()
    expect(menuText()).toBe('不感兴趣')

    await clickMenuItem()
    expect(wrapper.emitted('dislike')).toBeUndefined()
  })

  it('非 dismissible 的卡片（电影库首页）没有这个菜单', () => {
    mountCard({})
    expect(wrapper.find('.card-more').exists()).toBe(false)
  })
})
