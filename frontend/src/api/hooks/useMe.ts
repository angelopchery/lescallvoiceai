import { useQuery } from '@tanstack/react-query'
import { api } from '../client'

export type Me = {
  mode: 'single-tenant' | 'multi-tenant'
  email?: string
  name?: string
  picture?: string
  login_required?: boolean
}

/** The signed-in identity from /api/auth/me. Every user has the same
 * status — there is no admin tier. */
export function useMe() {
  return useQuery<Me>({
    queryKey: ['auth', 'me'],
    queryFn: () => api<Me>('/auth/me'),
    staleTime: 5 * 60_000,
    retry: false,
  })
}
