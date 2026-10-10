import http from './index'

/**
 * 获取当前用户的个性化推荐。
 * @param {string} [model] 'als'（默认，缺省不发参数）或 'itemcf'
 */
export const apiRecommendations = {
  get: (model) =>
    http.get('/recommendations/', {
      params: model && model !== 'als' ? { model } : {},
    }),
}
