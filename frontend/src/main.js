import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import './workspace.css'
import router from './router'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'

createApp(App).use(router).use(ElementPlus).mount('#app')
