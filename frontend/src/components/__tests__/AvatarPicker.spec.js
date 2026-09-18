/**
 * 头像选择面板：内置 SVG 头像网格 + 恢复默认。
 *
 * 这里只管「选哪个由谁负责」——落库由个人主页调接口完成，所以断言打在 emit 上。
 */
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import AvatarPicker from '@/components/AvatarPicker.vue'

const PRESETS = [
  { id: 'film', name: '胶片', url: '/api/profile/avatars/film.svg' },
  { id: 'star', name: '星星', url: '/api/profile/avatars/star.svg' },
]

function mountPicker(props = {}) {
  return mount(AvatarPicker, {
    props: { presets: PRESETS, current: '', saving: false, ...props },
    global: { plugins: [ElementPlus] },
  })
}

function resetButton(wrapper) {
  return wrapper.findAll('button').find((button) => button.text().includes('恢复默认'))
}

describe('头像选择面板', () => {
  it('把清单里的每个头像都画出来（图片地址就是接口给的 url）', () => {
    const wrapper = mountPicker()
    const items = wrapper.findAll('.preset-item')
    expect(items).toHaveLength(PRESETS.length)
    items.forEach((item, index) => {
      expect(item.find('img').attributes('src')).toBe(PRESETS[index].url)
      expect(item.attributes('title')).toBe(PRESETS[index].name)
    })
  })

  it('当前用的那个高亮，其余不高亮', () => {
    const wrapper = mountPicker({ current: PRESETS[1].url })
    const items = wrapper.findAll('.preset-item')
    expect(items[0].classes()).not.toContain('is-current')
    expect(items[1].classes()).toContain('is-current')
  })

  it('点某个头像 → emit select，带上它的 url', async () => {
    const wrapper = mountPicker()
    await wrapper.findAll('.preset-item')[0].trigger('click')
    expect(wrapper.emitted('select')[0]).toEqual([PRESETS[0].url])
  })

  it('已用内置头像时「恢复默认」可点，点了 emit reset', async () => {
    const wrapper = mountPicker({ current: PRESETS[0].url })
    const reset = resetButton(wrapper)
    expect(reset.attributes('disabled')).toBeUndefined()
    await reset.trigger('click')
    expect(wrapper.emitted('reset')).toHaveLength(1)
  })

  it('当前是历史上传头像时也能恢复默认（老数据要能被换掉）', () => {
    const wrapper = mountPicker({ current: '/api/profile/avatar/avatar_7_abc.png' })
    expect(resetButton(wrapper).attributes('disabled')).toBeUndefined()
    expect(wrapper.find('.current-hint').text()).toContain('历史头像')
  })

  it('当前已是默认头像时「恢复默认」禁用（没什么可恢复的）', () => {
    const wrapper = mountPicker({ current: '' })
    expect(resetButton(wrapper).attributes('disabled')).toBeDefined()
  })

  it('保存中时头像不可点，避免重复提交', () => {
    const wrapper = mountPicker({ saving: true })
    for (const item of wrapper.findAll('.preset-item')) {
      expect(item.attributes('disabled')).toBeDefined()
    }
  })
})
