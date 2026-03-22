import axios from '../axios'

export interface QRLoginStartResponse {
  success: boolean
  qrcode_url: string
  session_key: string
  message: string
}

export interface QRLoginWaitResponse {
  success: boolean
  status: number // 0=等待, 1=已扫码, 2=已确认, 3=过期
  message: string
  account_id: string
  bot_token: string
}

export interface OpenClawStatus {
  connected: boolean
  account_id: string
  monitor_running: boolean
  session_paused: boolean
  session_remaining_seconds: number
}

export interface LogoutResponse {
  success: boolean
  message: string
}

export const wechatOpenClawApi = {
  /** 获取登录二维码 */
  startQRLogin: async (): Promise<QRLoginStartResponse> => {
    const { data } = await axios.post<QRLoginStartResponse>(
      '/adapters/wechat_openclaw/login/qr/start'
    )
    return data
  },

  /** 等待扫码确认（长轮询） */
  waitQRLogin: async (sessionKey: string): Promise<QRLoginWaitResponse> => {
    const { data } = await axios.post<QRLoginWaitResponse>(
      '/adapters/wechat_openclaw/login/qr/wait',
      null,
      { params: { session_key: sessionKey }, timeout: 60000 }
    )
    return data
  },

  /** 获取连接状态 */
  getStatus: async (): Promise<OpenClawStatus> => {
    const { data } = await axios.get<OpenClawStatus>(
      '/adapters/wechat_openclaw/status'
    )
    return data
  },

  /** 登出 */
  logout: async (): Promise<LogoutResponse> => {
    const { data } = await axios.post<LogoutResponse>(
      '/adapters/wechat_openclaw/logout'
    )
    return data
  },
}
