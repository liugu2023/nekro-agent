import { Navigate } from 'react-router-dom'
import { useAuthStore } from '../../stores/auth'

interface RequireRoleProps {
  minRole: number
  children: React.ReactNode
}

export default function RequireRole({ minRole, children }: RequireRoleProps) {
  const { userInfo } = useAuthStore()
  const permLevel = userInfo?.perm_level ?? 0

  if (permLevel < minRole) {
    return <Navigate to="/dashboard" replace />
  }

  return <>{children}</>
}
