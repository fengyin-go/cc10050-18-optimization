import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import CrawlerWorkbench from '@/views/CrawlerWorkbench.vue'
import ScheduleWorkbench from '@/views/ScheduleWorkbench.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 采集调度/爬取任务需要轮次与页面视图，用独立工作台承载；其余模块仍走通用工作台。
    { path: '/crawl_schedule', name: 'crawl_schedule', component: ScheduleWorkbench },
    { path: '/price_crawler', name: 'price_crawler', component: CrawlerWorkbench },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
