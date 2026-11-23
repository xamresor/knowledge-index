// The default theme plus one thing: the home hero's background. KbRain.vue animates it (drops on water,
// rings that light up a code graph or a field of vectors); hero-bg.svg (drawn by Codex) is the still
// version for "reduce motion" and before the script runs.
import DefaultTheme from 'vitepress/theme'
import { h } from 'vue'
import KbRain from './KbRain.vue'
import './custom.css'

export default {
  extends: DefaultTheme,
  Layout() {
    return h(DefaultTheme.Layout, null, { 'home-hero-before': () => h(KbRain) })
  },
}
