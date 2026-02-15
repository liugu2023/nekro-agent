import { useAuthStore } from '../stores/auth'

export function useIsAdmin(): boolean {
  const { userInfo } = useAuthStore()
  return (userInfo?.perm_level ?? 0) >= 2
}
