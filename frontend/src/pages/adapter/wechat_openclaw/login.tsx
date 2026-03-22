import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Box,
  Card,
  CardContent,
  Typography,
  Alert,
  CircularProgress,
  Stack,
  Chip,
} from '@mui/material'
import { LoadingButton } from '@mui/lab'
import {
  QrCode2 as QrCodeIcon,
  CheckCircle as CheckCircleIcon,
  Logout as LogoutIcon,
  Refresh as RefreshIcon,
  WifiOff as WifiOffIcon,
  Wifi as WifiIcon,
  Pause as PauseIcon,
} from '@mui/icons-material'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { QRCodeSVG } from 'qrcode.react'
import { wechatOpenClawApi } from '../../../services/api/adapters/wechat_openclaw'
import { useNotification } from '../../../hooks/useNotification'
import { CARD_VARIANTS } from '../../../theme/variants'

export default function WeChatOpenClawLoginPage() {
  const notification = useNotification()
  const queryClient = useQueryClient()

  const [qrcodeUrl, setQrcodeUrl] = useState('')
  const [sessionKey, setSessionKey] = useState('')
  const [loginPhase, setLoginPhase] = useState<
    'idle' | 'loading_qr' | 'waiting_scan' | 'scanned' | 'success' | 'error'
  >('idle')
  const [loginMessage, setLoginMessage] = useState('')
  const [isLoggingOut, setIsLoggingOut] = useState(false)

  const waitingRef = useRef(false)

  // 查询连接状态
  const { data: status, isLoading: statusLoading } = useQuery({
    queryKey: ['wechat-openclaw-status'],
    queryFn: () => wechatOpenClawApi.getStatus(),
    refetchInterval: 5000,
  })

  // 等待扫码确认的轮询
  const pollQRStatus = useCallback(
    async (key: string) => {
      if (waitingRef.current) return
      waitingRef.current = true

      try {
        while (waitingRef.current) {
          const resp = await wechatOpenClawApi.waitQRLogin(key)

          if (resp.status === 2 && resp.success) {
            setLoginPhase('success')
            setLoginMessage('登录成功')
            notification.success('微信登录成功')
            queryClient.invalidateQueries({ queryKey: ['wechat-openclaw-status'] })
            waitingRef.current = false
            return
          }

          if (resp.status === 1) {
            setLoginPhase('scanned')
            setLoginMessage('已扫码，请在手机上确认')
            continue
          }

          if (resp.status === 3) {
            // 过期，停止等待
            setLoginPhase('error')
            setLoginMessage('二维码已过期，请重新获取')
            waitingRef.current = false
            return
          }

          // status === 0, 继续等待
        }
      } catch {
        if (waitingRef.current) {
          setLoginPhase('error')
          setLoginMessage('等待扫码超时，请重试')
        }
      } finally {
        waitingRef.current = false
      }
    },
    [notification, queryClient]
  )

  // 清理函数
  useEffect(() => {
    return () => {
      waitingRef.current = false
    }
  }, [])

  // 开始扫码登录
  const handleStartLogin = async () => {
    setLoginPhase('loading_qr')
    setLoginMessage('')
    waitingRef.current = false

    try {
      const resp = await wechatOpenClawApi.startQRLogin()
      if (resp.success && resp.qrcode_url) {
        setQrcodeUrl(resp.qrcode_url)
        setSessionKey(resp.session_key)
        setLoginPhase('waiting_scan')
        setLoginMessage('请使用微信扫描二维码')
        // 开始轮询扫码状态
        pollQRStatus(resp.session_key)
      } else {
        setLoginPhase('error')
        setLoginMessage(resp.message || '获取二维码失败')
      }
    } catch (e: unknown) {
      setLoginPhase('error')
      setLoginMessage(`获取二维码失败: ${e instanceof Error ? e.message : '未知错误'}`)
    }
  }

  // 登出
  const handleLogout = async () => {
    setIsLoggingOut(true)
    try {
      const resp = await wechatOpenClawApi.logout()
      if (resp.success) {
        notification.success('已登出')
        queryClient.invalidateQueries({ queryKey: ['wechat-openclaw-status'] })
        setLoginPhase('idle')
        setQrcodeUrl('')
        setSessionKey('')
      } else {
        notification.error(resp.message || '登出失败')
      }
    } catch {
      notification.error('登出失败')
    } finally {
      setIsLoggingOut(false)
    }
  }

  const isConnected = status?.connected ?? false

  return (
    <Box sx={{ p: 2, height: '100%', overflow: 'auto' }}>
      {/* 连接状态卡片 */}
      <Card sx={{ ...CARD_VARIANTS.default.styles, mb: 3 }}>
        <CardContent sx={{ p: 3 }}>
          <Stack direction="row" alignItems="center" spacing={2} sx={{ mb: 2 }}>
            {statusLoading ? (
              <CircularProgress size={20} />
            ) : isConnected ? (
              <WifiIcon color="success" />
            ) : (
              <WifiOffIcon color="disabled" />
            )}
            <Typography variant="h6" sx={{ fontWeight: 600 }}>
              连接状态
            </Typography>
          </Stack>

          {status && (
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              <Chip
                label={isConnected ? '已连接' : '未连接'}
                color={isConnected ? 'success' : 'default'}
                size="small"
                variant="outlined"
              />
              {status.monitor_running && (
                <Chip label="消息监听中" color="info" size="small" variant="outlined" />
              )}
              {status.session_paused && (
                <Chip
                  icon={<PauseIcon />}
                  label={`会话暂停 (${Math.ceil(status.session_remaining_seconds / 60)}分钟后恢复)`}
                  color="warning"
                  size="small"
                  variant="outlined"
                />
              )}
              {status.account_id && (
                <Chip label={`ID: ${status.account_id}`} size="small" variant="outlined" />
              )}
            </Stack>
          )}
        </CardContent>
      </Card>

      {/* 登录/登出操作 */}
      <Card sx={{ ...CARD_VARIANTS.default.styles }}>
        <CardContent sx={{ p: 3 }}>
          {isConnected ? (
            /* 已连接 - 显示登出按钮 */
            <Box>
              <Alert severity="success" sx={{ mb: 2 }} icon={<CheckCircleIcon />}>
                微信已登录，账号 ID: {status?.account_id}
              </Alert>
              <LoadingButton
                variant="outlined"
                color="error"
                startIcon={<LogoutIcon />}
                loading={isLoggingOut}
                onClick={handleLogout}
              >
                登出
              </LoadingButton>
            </Box>
          ) : (
            /* 未连接 - 显示扫码登录 */
            <Box>
              <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
                扫码登录
              </Typography>

              {loginPhase === 'idle' && (
                <LoadingButton
                  variant="contained"
                  startIcon={<QrCodeIcon />}
                  onClick={handleStartLogin}
                  size="large"
                >
                  获取登录二维码
                </LoadingButton>
              )}

              {loginPhase === 'loading_qr' && (
                <Stack alignItems="center" spacing={2} sx={{ py: 4 }}>
                  <CircularProgress />
                  <Typography color="text.secondary">正在获取二维码...</Typography>
                </Stack>
              )}

              {(loginPhase === 'waiting_scan' || loginPhase === 'scanned') && qrcodeUrl && (
                <Stack alignItems="center" spacing={2} sx={{ py: 2 }}>
                  <Box
                    sx={{
                      p: 2,
                      bgcolor: 'white',
                      borderRadius: 2,
                      display: 'inline-block',
                    }}
                  >
                    <QRCodeSVG
                      value={qrcodeUrl}
                      size={240}
                      level="M"
                    />
                  </Box>
                  <Alert
                    severity={loginPhase === 'scanned' ? 'success' : 'info'}
                    sx={{ width: '100%', maxWidth: 360 }}
                  >
                    {loginMessage}
                  </Alert>
                  {loginPhase === 'waiting_scan' && (
                    <LoadingButton
                      variant="text"
                      startIcon={<RefreshIcon />}
                      onClick={handleStartLogin}
                      size="small"
                    >
                      刷新二维码
                    </LoadingButton>
                  )}
                </Stack>
              )}

              {loginPhase === 'success' && (
                <Alert severity="success" icon={<CheckCircleIcon />}>
                  {loginMessage}
                </Alert>
              )}

              {loginPhase === 'error' && (
                <Stack spacing={2}>
                  <Alert severity="error">{loginMessage}</Alert>
                  <LoadingButton
                    variant="contained"
                    startIcon={<QrCodeIcon />}
                    onClick={handleStartLogin}
                  >
                    重新获取二维码
                  </LoadingButton>
                </Stack>
              )}
            </Box>
          )}
        </CardContent>
      </Card>
    </Box>
  )
}
