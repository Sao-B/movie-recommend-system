import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import AdminLayout from '@/layouts/AdminLayout.vue'

const routes = [
  { path: '/', name: 'home', component: () => import('@/views/HomeView.vue') },
  { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue') },
  { path: '/register', name: 'register', component: () => import('@/views/RegisterView.vue') },
  { path: '/movies/:id', name: 'movie-detail', component: () => import('@/views/MovieDetailView.vue') },
  {
    path: '/recommendations',
    name: 'recommendations',
    component: () => import('@/views/RecommendationsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/profile',
    name: 'profile',
    component: () => import('@/views/ProfileView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/admin',
    component: AdminLayout,
    meta: { requiresAuth: true, requiresAdmin: true },
    redirect: '/admin/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'admin-dashboard',
        component: () => import('@/views/admin/AdminDashboard.vue'),
        meta: { requiresAuth: true, requiresAdmin: true }
      },
      {
        path: 'users',
        name: 'admin-users',
        component: () => import('@/views/admin/AdminUsers.vue'),
        meta: { requiresAuth: true, requiresAdmin: true }
      },
      {
        path: 'movies',
        name: 'admin-movies',
        component: () => import('@/views/admin/AdminMovies.vue'),
        meta: { requiresAuth: true, requiresAdmin: true }
      },
      {
        path: 'ratings',
        name: 'admin-ratings',
        component: () => import('@/views/admin/AdminRatings.vue'),
        meta: { requiresAuth: true, requiresAdmin: true }
      },
      {
        path: 'models',
        name: 'admin-models',
        component: () => import('@/views/admin/AdminModels.vue'),
        meta: { requiresAuth: true, requiresAdmin: true }
      }
    ]
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const auth = useAuthStore()

  // 检查需要登录的路由
  if (to.meta.requiresAuth && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  // 检查需要管理员权限的路由（静默重定向，不显示错误提示）
  if (to.meta.requiresAdmin && !auth.isAdmin) {
    return { name: 'home' }
  }

  // 已登录用户访问登录/注册页面，重定向到首页
  if ((to.name === 'login' || to.name === 'register') && auth.isLoggedIn) {
    return { name: 'home' }
  }
})

export default router
