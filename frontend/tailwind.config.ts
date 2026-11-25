import type { Config } from 'tailwindcss'

export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Inter Variable"', 'Inter', 'system-ui', 'sans-serif'],
        display: ['"Plus Jakarta Sans Variable"', '"Plus Jakarta Sans"', 'Inter', 'sans-serif'],
      },
      // The app type scale. Body baseline is 13px (text-sm) — shadcn components
      // use text-sm/text-xs and inherit the scale automatically. Arbitrary
      // text-[Npx] sizes are being migrated onto these tokens; don't add new ones.
      fontSize: {
        '2xs': ['11px', { lineHeight: '16px' }],
        xs: ['12px', { lineHeight: '18px' }],
        sm: ['13px', { lineHeight: '20px' }],
        base: ['14px', { lineHeight: '22px' }],
        lg: ['16px', { lineHeight: '24px' }],
        xl: ['18px', { lineHeight: '26px' }],
        '2xl': ['20px', { lineHeight: '28px' }],
        '3xl': ['24px', { lineHeight: '32px' }],
        // Display tier — metric values only (StatCard, analytics tiles).
        // Never body copy; these are the numbers you read across the room.
        '4xl': ['30px', { lineHeight: '36px' }],
        '5xl': ['40px', { lineHeight: '44px' }],
      },
      colors: {
        // "mint" kept as the legacy name — values are now a refined emerald
        // so every existing mint-* usage inherits the calmer palette.
        mint: {
          DEFAULT: '#10b981',
          50: '#ecfdf5', 100: '#d1fae5', 200: '#a7f3d0', 300: '#6ee7b7',
          400: '#34d399', 500: '#10b981', 600: '#059669', 700: '#047857', 800: '#065f46', 900: '#064e3b',
        },
        indigo: {
          DEFAULT: '#4F46E5',
          50: '#eef2ff', 100: '#e0e7ff', 200: '#c7d2fe', 300: '#a5b4fc',
          400: '#818cf8', 500: '#6366f1', 600: '#4f46e5', 700: '#4338ca', 800: '#3730a3', 900: '#312e81',
        },
        ink: {
          DEFAULT: '#101014',
          soft: '#3f3f46',
          dim: '#6f7180',
        },
        // shadcn/ui tokens. --ui-border / --ui-accent are namespaced because the
        // app's own :root already owns --border (hairline rgba) and --accent
        // (emerald #00d98c) — letting shadcn share those names would bleed the
        // brand emerald into component hover states.
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        card: {
          DEFAULT: 'hsl(var(--card))',
          foreground: 'hsl(var(--card-foreground))',
        },
        popover: {
          DEFAULT: 'hsl(var(--popover))',
          foreground: 'hsl(var(--popover-foreground))',
        },
        primary: {
          DEFAULT: 'hsl(var(--primary))',
          foreground: 'hsl(var(--primary-foreground))',
        },
        secondary: {
          DEFAULT: 'hsl(var(--secondary))',
          foreground: 'hsl(var(--secondary-foreground))',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted))',
          foreground: 'hsl(var(--muted-foreground))',
        },
        accent: {
          DEFAULT: 'hsl(var(--ui-accent))',
          foreground: 'hsl(var(--ui-accent-foreground))',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive))',
          foreground: 'hsl(var(--destructive-foreground))',
        },
        border: 'hsl(var(--ui-border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
        chart: {
          1: 'hsl(var(--chart-1))',
          2: 'hsl(var(--chart-2))',
          3: 'hsl(var(--chart-3))',
          4: 'hsl(var(--chart-4))',
          5: 'hsl(var(--chart-5))',
        },
        sidebar: {
          DEFAULT: 'hsl(var(--sidebar))',
          foreground: 'hsl(var(--sidebar-foreground))',
          primary: 'hsl(var(--sidebar-primary))',
          'primary-foreground': 'hsl(var(--sidebar-primary-foreground))',
          accent: 'hsl(var(--sidebar-accent))',
          'accent-foreground': 'hsl(var(--sidebar-accent-foreground))',
          border: 'hsl(var(--sidebar-border))',
          ring: 'hsl(var(--sidebar-ring))',
        },
      },
      backgroundImage: {
        'mesh-light': 'radial-gradient(1200px 600px at 10% -10%, rgba(0,245,160,0.10), transparent 55%), radial-gradient(1000px 500px at 100% 0%, rgba(79,70,229,0.10), transparent 50%)',
        'mesh-dark': 'radial-gradient(1200px 600px at 10% -10%, rgba(0,245,160,0.12), transparent 55%), radial-gradient(1000px 500px at 100% 0%, rgba(79,70,229,0.18), transparent 50%)',
      },
      borderRadius: {
        xl: '12px', '2xl': '14px', '3xl': '18px',
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
      },
      boxShadow: {
        glass: '0 1px 2px rgba(16,16,20,0.04)',
        'glass-lg': '0 1px 2px rgba(16,16,20,0.04), 0 12px 32px -12px rgba(16,16,20,0.18)',
        // Legacy names, subdued: a hairline tinted ring + soft ambient.
        'glow-mint': '0 0 0 1px rgba(16,185,129,0.18), 0 1px 2px rgba(16,16,20,0.05)',
        'glow-indigo': '0 0 0 1px rgba(79,70,229,0.18), 0 1px 2px rgba(16,16,20,0.05)',
      },
      keyframes: {
        'fade-up': { '0%': { opacity: '0', transform: 'translateY(6px)' }, '100%': { opacity: '1', transform: 'translateY(0)' } },
        'slide-in-right': { '0%': { opacity: '0', transform: 'translateX(24px)' }, '100%': { opacity: '1', transform: 'translateX(0)' } },
        'pulse-soft': { '0%,100%': { opacity: '1' }, '50%': { opacity: '0.45' } },
        shimmer: { '100%': { transform: 'translateX(100%)' } },
      },
      animation: {
        'fade-up': 'fade-up .35s cubic-bezier(.22,1,.36,1) both',
        'slide-in-right': 'slide-in-right .3s cubic-bezier(.22,1,.36,1) both',
        'pulse-soft': 'pulse-soft 2s ease-in-out infinite',
      },
    },
  },
  plugins: [require('tailwindcss-animate')],
} satisfies Config
