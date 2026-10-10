"""合法电影类型枚举（用户偏好选择的校验用）。

与 MovieLens-32M 的标准类型一致（movies.genres 以 ``|`` 分隔的子集）。
前端选择列表来自 ``GET /api/movies/genres``（库里实际存在的类型），
校验用这份常量——两者高度重合，以常量为准可防止任意字符串入库。
"""

#: 标准类型（顺序即展示顺序）
CANONICAL_GENRES = (
    "Action", "Adventure", "Animation", "Children", "Comedy", "Crime",
    "Documentary", "Drama", "Fantasy", "Film-Noir", "Horror", "IMAX",
    "Musical", "Mystery", "Romance", "Sci-Fi", "Thriller", "War", "Western",
)

#: 用户偏好最多可选的类型数
MAX_FAVORITE_GENRES = 5
