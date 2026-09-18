/**
 * 登录页表单：填好后能带着两个字段发出请求。
 *
 * 这里替代的是原先 `tests/test_comment_ui_contract.py` 那种「读 .vue 源码文本」的断言。
 *
 * 已知限制（实测过，不写假装能测的断言）：
 * 想顺便断言「校验没通过时不发请求」，但在 jsdom 下 ElForm 的异步校验不落地 ——
 * 字段状态永远停在 "validating"、`validate()` 提前 resolve（async-validator 单独跑是能
 * 正确拒绝空串的），所以拿空表单去触发校验在本环境测不出真实行为；把表单的 validate
 * 换成桩也打不进组件内部的 `formRef`。这条路径因此没有自动化断言：
 * 代码里仍按后台页面的写法加了 `.catch(() => false)`（见 LoginView/RegisterView 的 submit），
 * 但没有测试锁着它 —— 不写一个「看着绿其实没测到」的用例。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import LoginView from '@/views/LoginView.vue'

vi.mock('@/api/auth', () => ({
  apiAuth: { login: vi.fn(), register: vi.fn() },
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: vi.fn() }),
  RouterLink: { template: '<a><slot /></a>' },
}))

import { apiAuth } from '@/api/auth'

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.clearAllMocks()
  apiAuth.login.mockResolvedValue({ token: 't', user: { id: 1, username: 'alice' } })
})

describe('登录页', () => {
  it('填好表单后点「登录」会带着两个字段发请求', async () => {
    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    const inputs = wrapper.findAll('input')
    await inputs[0].setValue('alice')
    await inputs[1].setValue('pw123456')
    await wrapper.find('button.full-btn').trigger('click')
    await flushPromises()

    expect(apiAuth.login).toHaveBeenCalledWith('alice', 'pw123456')
  })
})