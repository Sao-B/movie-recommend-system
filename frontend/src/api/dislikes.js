import http from './index'

/**
 * "不感兴趣"标记：推荐硬排除。
 * 标记过的电影不再出现在任何推荐链路（ItemCF / ALS / 热门兜底）里。
 */
export const apiDislikes = {
  /** 标记一部电影为不感兴趣（幂等） */
  add: (movieId) => http.post('/dislikes/', { movie_id: movieId }),
  /** 取消标记（幂等） */
  remove: (movieId) => http.delete(`/dislikes/${movieId}`),
  /** 我的全部标记：{ count, movie_ids: [..], items: [{movie_id,title,year,genres}] } */
  list: () => http.get('/dislikes/'),
}
