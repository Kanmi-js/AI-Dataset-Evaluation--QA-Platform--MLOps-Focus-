/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        pitch: {
          dark: '#080A08',
          card: '#111614',
          border: '#1C2621',
          hover: '#18211D',
        },
        volt: {
          DEFAULT: '#CCFF00',    // Electric high-viz trainer green
          dim: '#A3E635',
          glow: 'rgba(204, 255, 0, 0.18)',
        },
        vector: {
          cyan: '#06B6D4',       // Trajectory lines & tactical arrows
          crimson: '#FF334B',    // Fault/strain telemetry
          amber: '#F59E0B',      // Threshold warning
        }
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      boxShadow: {
        'volt-glow': '0 0 20px -3px rgba(204, 255, 0, 0.25)',
        'cyan-glow': '0 0 20px -3px rgba(6, 182, 212, 0.25)',
      }
    },
  },
  plugins: [],
}