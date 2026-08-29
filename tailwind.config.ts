import type { Config } from 'tailwindcss'

export default <Partial<Config>>{
  darkMode: 'class',
  content: [],
  theme: {
    extend: {
      colors: {
        py8n: {
          bg: '#0c0d10',
          panel: '#14161b',
          border: '#23262e',
          accent: '#f97316',
        },
      },
    },
  },
  plugins: [],
}
