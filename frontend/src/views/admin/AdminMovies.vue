<template>
  <div class="admin-movies">
    <el-card>
      <template #header>
        <span>电影管理</span>
      </template>

      <el-form :inline="true" :model="searchForm" class="search-form">
        <el-form-item label="电影标题">
          <el-input v-model="searchForm.title" placeholder="请输入电影标题" clearable />
        </el-form-item>
        <el-form-item label="类型">
          <el-input v-model="searchForm.genres" placeholder="请输入类型" clearable />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">搜索</el-button>
          <el-button @click="handleReset">重置</el-button>
        </el-form-item>
      </el-form>

      <el-table :data="movies" v-loading="loading" border stripe>
        <el-table-column prop="movie_id" label="ID" width="100" />
        <el-table-column prop="title" label="标题" min-width="200" />
        <el-table-column prop="genres" label="类型" width="200" />
        <el-table-column prop="year" label="年份" width="100" />
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button size="small" @click="showEditDialog(row)">编辑</el-button>
            <el-button size="small" type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="pagination.page"
        v-model:page-size="pagination.page_size"
        :total="pagination.total"
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        @size-change="loadMovies"
        @current-change="loadMovies"
        style="margin-top: 20px; justify-content: center;"
      />
    </el-card>

    <!-- 编辑对话框 -->
    <el-dialog v-model="dialogVisible" title="编辑电影" width="500px">
      <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
        <el-form-item label="电影ID">
          <el-input v-model="form.movie_id" disabled />
        </el-form-item>
        <el-form-item label="标题" prop="title">
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item label="类型" prop="genres">
          <el-input v-model="form.genres" />
        </el-form-item>
        <el-form-item label="年份" prop="year">
          <el-input-number v-model="form.year" :min="1900" :max="2100" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSubmit">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { apiAdmin } from '@/api/admin'
import { ElMessage, ElMessageBox } from 'element-plus'

const loading = ref(false)
const movies = ref([])
const pagination = reactive({
  page: 1,
  page_size: 10,
  total: 0
})

const searchForm = reactive({
  title: '',
  genres: ''
})

const dialogVisible = ref(false)
const formRef = ref(null)
const form = reactive({
  movie_id: null,
  title: '',
  genres: '',
  year: null
})

const rules = {
  title: [
    { required: true, message: '请输入电影标题', trigger: 'blur' }
  ],
  genres: [
    { required: true, message: '请输入类型', trigger: 'blur' }
  ],
  year: [
    { required: true, message: '请输入年份', trigger: 'blur' }
  ]
}

const loadMovies = async () => {
  loading.value = true
  try {
    const params = {
      page: pagination.page,
      page_size: pagination.page_size,
      ...searchForm
    }
    const data = await apiAdmin.listMovies(params)
    movies.value = data.movies
    pagination.total = data.total
  } catch (error) {
    ElMessage.error('加载电影列表失败')
  } finally {
    loading.value = false
  }
}

const handleSearch = () => {
  pagination.page = 1
  loadMovies()
}

const handleReset = () => {
  searchForm.title = ''
  searchForm.genres = ''
  pagination.page = 1
  loadMovies()
}

const showEditDialog = (row) => {
  Object.assign(form, {
    movie_id: row.movie_id,
    title: row.title,
    genres: row.genres,
    year: row.year
  })
  dialogVisible.value = true
}

const handleSubmit = async () => {
  if (!formRef.value) return

  await formRef.value.validate(async (valid) => {
    if (!valid) return

    try {
      await apiAdmin.updateMovie(form.movie_id, {
        title: form.title,
        genres: form.genres,
        year: form.year
      })
      ElMessage.success('更新电影成功')
      dialogVisible.value = false
      loadMovies()
    } catch (error) {
      ElMessage.error('更新电影失败')
    }
  })
}

const handleDelete = async (row) => {
  try {
    await ElMessageBox.confirm(`确定删除电影 "${row.title}" 吗?`, '警告', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })
    await apiAdmin.deleteMovie(row.movie_id)
    ElMessage.success('删除成功')
    loadMovies()
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

onMounted(() => {
  loadMovies()
})
</script>

<style scoped>
.admin-movies {
  width: 100%;
}

.search-form {
  margin-bottom: 20px;
}
</style>
