/**
 * API 客户端的「连不上后端」处理 —— 真实 axios 实例 + 替身 adapter。
 *
 * 背景：vite 两秒就绪、后端冷启动要 ~20s，页面可能先发请求。
 * 这里锁住的是：GET 会退避重试两次、超时与写请求不重试、最终失败时提示写清楚。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AxiosError } from 'axios'

vi.mock('element-plus', () => ({ ElMessage: { error: vi.fn() } }))

import { ElMessage } from 'element-plus'
import http from '@/api/index'

const NETWORK_ERROR_TEXT = '连不上后端服务，请稍后重试（后端可能还在启动）'

function ok(config) {
  return { data: { ok: true }, status: 200, statusText: 'OK', headers: {}, config }
}

/** 前 failTimes 次调用按 failCode 失败，之后成功；记录总调用次数。 */
function adapter({ failTimes = 0, failCode = AxiosError.ERR_NETWORK } = {}) {
  const state = { calls: 0 }
  state.fn = (config) => {
    state.calls += 1
    if (state.calls <= failTimes) {
      return Promise.reject(new AxiosError('boom', failCode, config))
    }
    return Promise.resolve(ok(config))
  }
  return state
}

/** 让退避的那两次 setTimeout 走完（1s、2s） */
async function drainRetries() {
  await Promise.resolve()
  await vi.advanceTimersByTimeAsync(1000)
  await vi.advanceTimersByTimeAsync(2000)
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  localStorage.clear()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('API 客户端：后端还没起来时', () => {
  it('GET 会退避重试两次，第三次成功就照常返回数据', async () => {
    const mock = adapter({ failTimes: 2 })
    http.defaults.adapter = mock.fn

    const promise = http.get('/movies/')
    await drainRetries()

    await expect(promise).resolves.toEqual({ ok: true })
    expect(mock.calls).toBe(3)
    expect(ElMessage.error).not.toHaveBeenCalled()
  })

  it('写请求（POST）不重试 —— 连不上就立刻失败，免得重复提交', async () => {
    const mock = adapter({ failTimes: 5 })
    http.defaults.adapter = mock.fn

    const promise = http.post('/ratings/', { movie_id: 318, rating: 4.5 })
    const assertion = expect(promise).rejects.toBe(NETWORK_ERROR_TEXT)
    await drainRetries()
    await assertion

    expect(mock.calls).toBe(1)
    expect(ElMessage.error).toHaveBeenCalledWith(NETWORK_ERROR_TEXT)
  })

  it('超时（ECONNABORTED）不重试 —— 那是后端慢，不是没起来', async () => {
    const mock = adapter({ failTimes: 5, failCode: AxiosError.ECONNABORTED })
    http.defaults.adapter = mock.fn

    const promise = http.get('/movies/')
    const assertion = expect(promise).rejects.toBe('请求失败，请重试')
    await drainRetries()
    await assertion

    expect(mock.calls).toBe(1)
  })

  it('重试用尽仍然失败时，提示说明后端可能还在启动', async () => {
    const mock = adapter({ failTimes: 99 })
    http.defaults.adapter = mock.fn

    const promise = http.get('/movies/')
    const assertion = expect(promise).rejects.toBe(NETWORK_ERROR_TEXT)
    await drainRetries()
    await assertion

    expect(mock.calls).toBe(3)   // 首次 + 2 次重试
    expect(ElMessage.error).toHaveBeenCalledWith(NETWORK_ERROR_TEXT)
  })
})
