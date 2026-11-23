import { defineConfig } from 'vitepress'

// The documentation site: knowledge-index.sereda.lv. Source: the Markdown in docs/ (the same files the
// KB itself indexes). Build: `npm run docs:build` in the repository root -> docs/.vitepress/dist.
const REPO = 'https://github.com/xamresor/knowledge-index'

// Links from docs/ to files outside it (../README.md, ../skills/...) point to GitHub on the site, and
// links written from the repository root (docs/server.md, in the included CHANGELOG) to the page itself.
function repoLinks(md: any) {
  const open = md.renderer.rules.link_open || ((t: any, i: number, o: any, e: any, s: any) => s.renderToken(t, i, o))
  md.renderer.rules.link_open = (tokens: any, idx: number, opts: any, env: any, self: any) => {
    const href: string = tokens[idx].attrGet('href') || ''
    if (href.startsWith('../')) {
      tokens[idx].attrSet('href', `${REPO}/blob/main/${href.replace(/^(\.\.\/)+/, '')}`)
    } else if (href.startsWith('docs/')) {
      tokens[idx].attrSet('href', '/' + href.slice(5).replace(/\.md(#|$)/, '$1'))
    }
    return open(tokens, idx, opts, env, self)
  }
}

export default defineConfig({
  markdown: { config: repoLinks },
  ignoreDeadLinks: [/^\.\/\.\.\//, /^\.\/docs\//],
  title: 'Knowledge Index',
  description: 'GraphRAG for code: one search over related code projects and their docs, for AI agents.',
  lang: 'en-US',
  cleanUrls: true,
  lastUpdated: true,
  srcExclude: ['**/README.md'],
  head: [['meta', { name: 'theme-color', content: '#2563eb' }]],
  themeConfig: {
    nav: [
      { text: 'Guide', link: '/getting-started' },
      { text: 'Configuration', link: '/configuration' },
      { text: 'Changelog', link: '/changelog' },
    ],
    sidebar: [
      { text: 'Introduction', items: [
        { text: 'Getting started', link: '/getting-started' },
        { text: 'Tools and add-ons', link: '/tools' },
        { text: 'Data dictionary', link: '/data-dictionary' },
      ] },
      { text: 'Installation', items: [
        { text: 'Install and configure', link: '/install' },
        { text: 'Configuration reference', link: '/configuration' },
      ] },
      { text: 'Shared server', items: [
        { text: 'The shared server', link: '/server' },
        { text: 'Docker', link: '/docker' },
        { text: 'Access protocol', link: '/access-protocol' },
      ] },
      { text: 'Writing docs', items: [
        { text: 'Documentation guide', link: '/documentation-guide' },
      ] },
      { text: 'Appendix', items: [
        { text: 'Design and roadmap', link: '/design' },
        { text: 'Changelog', link: '/changelog' },
      ] },
    ],
    outline: { level: [2, 3], label: 'On this page' },
    search: { provider: 'local' },
    socialLinks: [{ icon: 'github', link: 'https://github.com/xamresor/knowledge-index' }],
    editLink: { pattern: 'https://github.com/xamresor/knowledge-index/edit/main/docs/:path' },
    footer: { message: 'AGPL-3.0-or-later', copyright: '© 2026 Romans Sereda' },
  },
})
