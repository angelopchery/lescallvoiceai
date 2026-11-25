import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MotionConfig } from 'motion/react'
import App from './App'
import { ThemeProvider } from './theme/ThemeProvider'
import { ToastProvider } from './components/Toast'
import { ConfirmProvider } from './components/confirm'
import { SmoothScroll } from './components/SmoothScroll'
import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 15_000 },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        {/* reducedMotion="user": every motion/react animation collapses to an
            instant cut for users who ask the OS for less motion. */}
        <MotionConfig reducedMotion="user">
          <SmoothScroll>
            <ToastProvider>
              <ConfirmProvider>
                <BrowserRouter>
                  <App />
                </BrowserRouter>
              </ConfirmProvider>
            </ToastProvider>
          </SmoothScroll>
        </MotionConfig>
      </QueryClientProvider>
    </ThemeProvider>
  </React.StrictMode>,
)
