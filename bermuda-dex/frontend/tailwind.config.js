/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        darkBg: '#0f1117',
        cardBg: '#1a1d24',
        baseBlue: '#0052ff',
        baseBlueHover: '#0043d9',
        borderDark: '#2a2f3a',
      }
    },
  },
  plugins: [],
}
