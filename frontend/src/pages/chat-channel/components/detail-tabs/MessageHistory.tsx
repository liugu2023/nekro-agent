import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import {
  Box,
  Typography,
  Avatar,
  CircularProgress,
  useTheme,
  Button,
  Dialog,
  TextField,
  IconButton,
  Snackbar,
  Alert,
  Tooltip,
  Chip,
  Card,
  CardMedia,
  CardContent,
  Popover,
  List,
  ListItem,
  ListItemButton,
  ListItemText,
  ToggleButtonGroup,
  ToggleButton,
} from '@mui/material'
import SendIcon from '@mui/icons-material/Send'
import AttachFileIcon from '@mui/icons-material/AttachFile'
import CloseIcon from '@mui/icons-material/Close'
import ReplyIcon from '@mui/icons-material/Reply'
import PictureAsPdfIcon from '@mui/icons-material/PictureAsPdf'
import DescriptionIcon from '@mui/icons-material/Description'
import AudioFileIcon from '@mui/icons-material/AudioFile'
import VideoFileIcon from '@mui/icons-material/VideoFile'
import FolderZipIcon from '@mui/icons-material/FolderZip'
import InsertDriveFileIcon from '@mui/icons-material/InsertDriveFile'
import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query'
import { chatChannelApi, ChatMessage, ChatMessageSegment } from '../../../../services/api/chat-channel'
import { useTranslation } from 'react-i18next'

// 防抖函数
function debounce<T extends (...args: unknown[]) => unknown>(
  fn: T,
  delay: number
): (...args: Parameters<T>) => void {
  let timeoutId: number
  return (...args: Parameters<T>) => {
    window.clearTimeout(timeoutId)
    timeoutId = window.setTimeout(() => fn(...args), delay)
  }
}

interface MessageHistoryProps {
  chatKey: string
  canSend?: boolean
  aiAlwaysIncludeMsgId?: boolean
}

interface MessageResponse {
  total: number
  items: ChatMessage[]
}

/** Bot 的 sender_id 固定为 "-1" */
const BOT_SENDER_ID = '-1'

/** 根据 platform_userid 生成 QQ 头像 URL */
function getAvatarUrl(platformUserid: string): string | undefined {
  if (!platformUserid || platformUserid === '-1') return undefined
  // QQ 头像
  if (/^\d{5,}$/.test(platformUserid)) {
    return `https://q1.qlogo.cn/g?b=qq&nk=${platformUserid}&s=100`
  }
  return undefined
}

/** 判断两条消息之间是否需要时间分隔（>5 分钟） */
function needTimeDivider(prev: ChatMessage, curr: ChatMessage): boolean {
  const t1 = new Date(prev.create_time).getTime()
  const t2 = new Date(curr.create_time).getTime()
  return t2 - t1 > 5 * 60 * 1000
}

/** 名字生成稳定色相 */
function nameToColor(name: string): string {
  let hash = 0
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash)
  }
  const hue = Math.abs(hash) % 360
  return `hsl(${hue}, 55%, 55%)`
}

/** 从 local_path 提取文件名 */
function extractFileName(localPath: string): string {
  const parts = localPath.replace(/\\/g, '/').split('/')
  return parts[parts.length - 1] || ''
}

/** 构造上传文件的 API URL */
function getUploadUrl(chatKey: string, segment: ChatMessageSegment): string {
  const fileName = segment.file_name || (segment.local_path ? extractFileName(segment.local_path) : '')
  if (!fileName) return ''
  return `/api/common/uploads/${encodeURIComponent(chatKey)}/${encodeURIComponent(fileName)}`
}

/** 根据文件名获取文件类型信息（图标组件、颜色、标签） */
function getFileTypeInfo(fileName: string): { IconComponent: React.ElementType; color: string; label: string } {
  const ext = fileName.split('.').pop()?.toLowerCase() || ''
  switch (ext) {
    case 'pdf':
      return { IconComponent: PictureAsPdfIcon, color: '#e53935', label: 'PDF' }
    case 'doc': case 'docx':
      return { IconComponent: DescriptionIcon, color: '#1565c0', label: 'Word' }
    case 'xls': case 'xlsx':
      return { IconComponent: DescriptionIcon, color: '#2e7d32', label: 'Excel' }
    case 'ppt': case 'pptx':
      return { IconComponent: DescriptionIcon, color: '#d84315', label: 'PPT' }
    case 'zip': case 'rar': case '7z': case 'tar': case 'gz':
      return { IconComponent: FolderZipIcon, color: '#6d4c41', label: ext.toUpperCase() }
    case 'mp3': case 'wav': case 'flac': case 'ogg': case 'aac':
      return { IconComponent: AudioFileIcon, color: '#7b1fa2', label: ext.toUpperCase() }
    case 'mp4': case 'avi': case 'mkv': case 'mov': case 'webm':
      return { IconComponent: VideoFileIcon, color: '#c62828', label: ext.toUpperCase() }
    case 'txt': case 'md': case 'json': case 'csv': case 'log':
      return { IconComponent: DescriptionIcon, color: '#546e7a', label: ext.toUpperCase() }
    default:
      return { IconComponent: InsertDriveFileIcon, color: '#78909c', label: ext ? ext.toUpperCase() : 'FILE' }
  }
}

/** 文件卡片组件 */
function FileCard({
  segment,
  isDark,
}: {
  segment: ChatMessageSegment
  isDark: boolean
}) {
  const theme = useTheme()
  const fileName = segment.file_name || segment.text || 'file'
  const fileInfo = getFileTypeInfo(fileName)
  const FileIcon = fileInfo.IconComponent

  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'center',
        gap: 1.5,
        my: 0.5,
        p: 1.2,
        borderRadius: '10px',
        bgcolor: isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.03)',
        border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)'}`,
        minWidth: 200,
        maxWidth: 320,
      }}
    >
      {/* 文件图标 */}
      <Box
        sx={{
          width: 44,
          height: 44,
          borderRadius: '8px',
          bgcolor: `${fileInfo.color}18`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
          color: fileInfo.color,
        }}
      >
        <FileIcon sx={{ fontSize: 32 }} />
      </Box>

      {/* 文件信息 */}
      <Box sx={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
        <Tooltip title={fileName} placement="top">
          <Typography
            variant="body2"
            sx={{
              fontSize: '13px',
              fontWeight: 500,
              color: theme.palette.text.primary,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {fileName}
          </Typography>
        </Tooltip>
        <Typography
          variant="caption"
          sx={{
            fontSize: '11px',
            color: theme.palette.text.disabled,
          }}
        >
          {fileInfo.label}
        </Typography>
      </Box>
    </Box>
  )
}

/** @提及 Chip 组件 */
function AtMention({ segment, isDark }: { segment: ChatMessageSegment; isDark: boolean }) {
  const theme = useTheme()
  return (
    <Chip
      label={`@${segment.target_nickname || 'User'}`}
      size="small"
      variant="outlined"
      sx={{
        fontWeight: 600,
        fontSize: '12px',
        height: 24,
        my: 0.3,
        bgcolor: isDark ? 'rgba(33, 150, 243, 0.15)' : 'rgba(33, 150, 243, 0.1)',
        borderColor: theme.palette.primary.main,
        color: theme.palette.primary.main,
        cursor: 'default',
      }}
    />
  )
}

/** JSON 卡片组件 */
function JsonCardComponent({
  segment,
  isDark,
}: {
  segment: ChatMessageSegment
  isDark: boolean
}) {
  const theme = useTheme()
  const cardTitle = segment.card_title || segment.text || '卡片'
  const cardDesc = segment.card_desc || ''
  const cardIcon = segment.card_icon || ''
  const cardPreview = segment.card_preview || ''
  const cardUrl = segment.card_url || ''

  return (
    <Card
      sx={{
        my: 0.5,
        maxWidth: 320,
        overflow: 'hidden',
        bgcolor: isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.02)',
        border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)'}`,
        transition: 'all 0.2s',
        cursor: cardUrl ? 'pointer' : 'default',
        '&:hover': cardUrl
          ? {
              bgcolor: isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.04)',
              boxShadow: theme.shadows[4],
            }
          : {},
      }}
      onClick={() => {
        if (cardUrl) window.open(cardUrl, '_blank')
      }}
    >
      {/* 卡片预览图 */}
      {cardPreview && (
        <CardMedia
          component="img"
          height={140}
          image={cardPreview}
          alt={cardTitle}
          sx={{ objectFit: 'cover' }}
        />
      )}

      {/* 卡片内容 */}
      <CardContent sx={{ p: 1.2, '&:last-child': { pb: 1.2 } }}>
        <Box sx={{ display: 'flex', gap: 0.8, alignItems: 'flex-start' }}>
          {/* 卡片图标 */}
          {cardIcon && (
            <img
              src={cardIcon}
              alt="icon"
              style={{
                width: 32,
                height: 32,
                borderRadius: 4,
                flexShrink: 0,
              }}
              onError={(e) => {
                const target = e.target as HTMLImageElement
                target.style.display = 'none'
              }}
            />
          )}

          {/* 卡片标题和描述 */}
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography
              variant="body2"
              sx={{
                fontWeight: 600,
                fontSize: '13px',
                color: theme.palette.text.primary,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                mb: 0.3,
              }}
            >
              {cardTitle}
            </Typography>
            {cardDesc && (
              <Typography
                variant="caption"
                sx={{
                  fontSize: '12px',
                  color: theme.palette.text.secondary,
                  display: '-webkit-box',
                  WebkitBoxOrient: 'vertical',
                  WebkitLineClamp: 2,
                  overflow: 'hidden',
                }}
              >
                {cardDesc}
              </Typography>
            )}
            {segment.share_from_nick && (
              <Typography
                variant="caption"
                sx={{
                  fontSize: '11px',
                  color: theme.palette.text.disabled,
                  display: 'block',
                  mt: 0.3,
                }}
              >
                来自: {segment.share_from_nick}
              </Typography>
            )}
          </Box>
        </Box>
      </CardContent>
    </Card>
  )
}

/** 渲染消息内容（支持图文混排） */
function MessageContent({
  message,
  noContentText,
}: {
  message: ChatMessage
  noContentText: string
}) {
  const theme = useTheme()
  const [previewSrc, setPreviewSrc] = useState<string | null>(null)

  const segments = message.content_data || []

  // 没有 content_data 时回退到纯文本
  if (segments.length === 0) {
    return (
      <>
        <Typography
          variant="body2"
          sx={{
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            overflowWrap: 'break-word',
            fontSize: '13.5px',
            lineHeight: 1.6,
            color: message.content
              ? theme.palette.text.primary
              : theme.palette.text.disabled,
            fontStyle: message.content ? 'normal' : 'italic',
          }}
        >
          {message.content || noContentText}
        </Typography>
      </>
    )
  }

  return (
    <>
      {segments.map((seg, i) => {
        if (seg.type === 'image') {
          const src = getUploadUrl(message.chat_key, seg)
          if (!src) return null
          return (
            <Box key={i} sx={{ my: 0.5 }}>
              <img
                src={src}
                alt={seg.file_name || 'image'}
                onClick={() => setPreviewSrc(src)}
                style={{
                  maxWidth: '100%',
                  maxHeight: 300,
                  borderRadius: 8,
                  cursor: 'pointer',
                  display: 'block',
                }}
                loading="lazy"
                onError={(e) => {
                  const target = e.target as HTMLImageElement
                  target.style.display = 'none'
                }}
              />
            </Box>
          )
        }

        if (seg.type === 'file' || seg.type === 'voice' || seg.type === 'video') {
          return (
            <FileCard
              key={i}
              segment={seg}
              isDark={theme.palette.mode === 'dark'}
            />
          )
        }

        if (seg.type === 'at') {
          return (
            <Box key={i} sx={{ display: 'inline-block', mr: 0.5 }}>
              <AtMention
                segment={seg}
                isDark={theme.palette.mode === 'dark'}
              />
            </Box>
          )
        }

        if (seg.type === 'json_card') {
          return (
            <JsonCardComponent
              key={i}
              segment={seg}
              isDark={theme.palette.mode === 'dark'}
            />
          )
        }

        // text：渲染文本
        if (seg.text) {
          return (
            <Typography
              key={i}
              variant="body2"
              component="span"
              sx={{
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                overflowWrap: 'break-word',
                fontSize: '13.5px',
                lineHeight: 1.6,
                color: theme.palette.text.primary,
              }}
            >
              {seg.text}
            </Typography>
          )
        }

        return null
      })}

      {/* 图片预览弹窗 */}
      {previewSrc && (
        <Dialog
          open
          onClose={() => setPreviewSrc(null)}
          maxWidth={false}
          PaperProps={{
            sx: {
              bgcolor: 'transparent',
              boxShadow: 'none',
              maxWidth: '90vw',
              maxHeight: '90vh',
            },
          }}
        >
          <img
            src={previewSrc}
            alt="preview"
            onClick={() => setPreviewSrc(null)}
            style={{
              maxWidth: '90vw',
              maxHeight: '90vh',
              objectFit: 'contain',
              cursor: 'pointer',
              borderRadius: 4,
            }}
          />
        </Dialog>
      )}
    </>
  )
}

export default function MessageHistory({ chatKey, canSend = false, aiAlwaysIncludeMsgId = false }: MessageHistoryProps) {
  const { t } = useTranslation('chat-channel')
  const theme = useTheme()
  const queryClient = useQueryClient()
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const [autoScroll, setAutoScroll] = useState(true)
  const loadMoreRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [initialLoad, setInitialLoad] = useState(true)
  const prevScrollHeightRef = useRef<number>(0)
  const isLoadingMoreRef = useRef(false)

  // 发送消息状态
  const [inputValue, setInputValue] = useState('')
  const [sending, setSending] = useState(false)
  const [senderType, setSenderType] = useState<'bot' | 'system' | 'none'>('bot')
  const [attachedFile, setAttachedFile] = useState<File | null>(null)
  const [filePreviewUrl, setFilePreviewUrl] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [snack, setSnack] = useState<{ open: boolean; message: string; severity: 'success' | 'error' }>({
    open: false,
    message: '',
    severity: 'success',
  })

  // @ 用户选择
  const [atAnchorEl, setAtAnchorEl] = useState<HTMLElement | null>(null)
  const [atUsers, setAtUsers] = useState<Array<{ platform_userid: string; nickname: string }>>([])
  const [atQuery, setAtQuery] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const atDebounceTimerRef = useRef<number | null>(null)
  const isComposingRef = useRef(false)

  // 清理防抖计时器
  useEffect(() => {
    return () => {
      if (atDebounceTimerRef.current) {
        clearTimeout(atDebounceTimerRef.current)
      }
    }
  }, [])

  const isDark = theme.palette.mode === 'dark'

  // 管理附件预览 Blob URL 生命周期
  useEffect(() => {
    if (!attachedFile || !attachedFile.type.startsWith('image/')) {
      setFilePreviewUrl(null)
      return
    }
    const url = URL.createObjectURL(attachedFile)
    setFilePreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [attachedFile])

  // 查询消息历史
  const { data, fetchNextPage, hasNextPage, isFetchingNextPage, isLoading } = useInfiniteQuery({
    queryKey: ['chat-messages', chatKey],
    initialPageParam: undefined as number | undefined,
    queryFn: async ({ pageParam }) => {
      const response = await chatChannelApi.getMessages({
        chat_key: chatKey,
        before_id: pageParam,
      })
      return response
    },
    getNextPageParam: (lastPage: MessageResponse) => {
      if (lastPage.items.length === 0) return undefined
      return lastPage.items[lastPage.items.length - 1].id
    },
  })

  // 实时消息流订阅 (SSE)
  useEffect(() => {
    let cleanup: (() => void) | undefined

    const handleNewMessage = (message: ChatMessage) => {
      // 将消息添加到 React Query 缓存的最后一页
      queryClient.setQueryData(['chat-messages', chatKey], (oldData: any) => {
        if (!oldData?.pages) return oldData

        const newPages = [...oldData.pages]
        const lastPage = { ...newPages[newPages.length - 1] }
        lastPage.items = [...lastPage.items, message]
        newPages[newPages.length - 1] = lastPage

        return { ...oldData, pages: newPages }
      })

      // 如果用户在底部，自动滚动到最新消息
      if (autoScroll) {
        setTimeout(() => messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 100)
      }
    }

    // 仅在启用 AI_ALWAYS_INCLUDE_MSG_ID 功能时订阅
    if (aiAlwaysIncludeMsgId && chatKey) {
      cleanup = chatChannelApi.streamMessages(chatKey, handleNewMessage, (error) => {
        console.error('Message stream error:', error)
      })
    }

    return () => cleanup?.()
  }, [chatKey, aiAlwaysIncludeMsgId, queryClient, autoScroll])

  // 自动滚动到底部（仅初始加载时）
  useEffect(() => {
    if (!isLoading && initialLoad && messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView()
      setInitialLoad(false)
    }
  }, [isLoading, initialLoad])

  // 处理加载更多
  const handleLoadMore = useCallback(() => {
    if (!hasNextPage || isFetchingNextPage || isLoadingMoreRef.current) return
    const container = containerRef.current
    if (!container) return

    isLoadingMoreRef.current = true
    prevScrollHeightRef.current = container.scrollHeight
    fetchNextPage().finally(() => {
      isLoadingMoreRef.current = false
    })
  }, [hasNextPage, isFetchingNextPage, fetchNextPage])

  // 处理滚动事件
  const handleScroll = useCallback(() => {
    const container = containerRef.current
    if (!container) return

    const { scrollHeight, scrollTop, clientHeight } = container

    const isNearBottom = scrollHeight - scrollTop - clientHeight < 100
    setAutoScroll(isNearBottom)

    if (scrollTop < 50 && !isFetchingNextPage && hasNextPage) {
      handleLoadMore()
    }
  }, [hasNextPage, isFetchingNextPage, handleLoadMore])

  // 监听滚动位置
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const debouncedScroll = debounce(handleScroll, 100)
    container.addEventListener('scroll', debouncedScroll)
    return () => container.removeEventListener('scroll', debouncedScroll)
  }, [handleScroll])

  // 保持滚动位置
  useEffect(() => {
    const container = containerRef.current
    if (!container || !data?.pages) return

    if (prevScrollHeightRef.current > 0) {
      const newScrollHeight = container.scrollHeight
      const scrollDiff = newScrollHeight - prevScrollHeightRef.current
      container.scrollTop = scrollDiff
      prevScrollHeightRef.current = 0
    }
  }, [data?.pages])

  // 处理回到底部
  const handleScrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    setAutoScroll(true)
  }, [])

  // 发送消息
  const handleSend = useCallback(async () => {
    const msg = inputValue.trim()
    if (!msg && !attachedFile) return
    if (sending) return
    setSending(true)
    try {
      const res = await chatChannelApi.sendMessage(chatKey, msg, attachedFile || undefined, senderType)
      if (res.ok) {
        setInputValue('')
        setAttachedFile(null)
        setSnack({ open: true, message: t('messageHistory.sendSuccess'), severity: 'success' })
        // 刷新消息列表
        await queryClient.invalidateQueries({ queryKey: ['chat-messages', chatKey] })
        // 滚动到底部
        setTimeout(() => messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 300)
      } else {
        setSnack({ open: true, message: `${t('messageHistory.sendFailed')}: ${res.error || ''}`, severity: 'error' })
      }
    } catch (e: unknown) {
      const errMsg = e instanceof Error ? e.message : String(e)
      setSnack({ open: true, message: `${t('messageHistory.sendFailed')}: ${errMsg}`, severity: 'error' })
    } finally {
      setSending(false)
    }
  }, [inputValue, attachedFile, sending, chatKey, senderType, queryClient, t])

  // 回车发送（IME 输入法确认时不触发）
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && !isComposingRef.current) {
        e.preventDefault()
        handleSend()
      }
    },
    [handleSend]
  )

  // 处理输入框变化（检测@符号，防抖匹配；IME 组合中跳过检测）
  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const value = e.target.value
      setInputValue(value)

      // IME 组合中不做 @mention 检测
      if (isComposingRef.current) return

      // 清除之前的防抖计时器
      if (atDebounceTimerRef.current) {
        clearTimeout(atDebounceTimerRef.current)
      }

      // 检测@符号
      const atIndex = value.lastIndexOf('@')
      if (atIndex >= 0) {
        const afterAt = value.slice(atIndex + 1)
        // 只显示输入中的@（没有空格）
        if (!afterAt.includes(' ') && !afterAt.includes('\n')) {
          setAtQuery(afterAt)

          // 防抖：延迟 500ms 后执行匹配
          atDebounceTimerRef.current = window.setTimeout(async () => {
            if (atIndex === 0 || value[atIndex - 1] === ' ' || value[atIndex - 1] === '\n') {
              try {
                const res = await chatChannelApi.getUsers(chatKey)
                setAtUsers(res.items)
                // 定位弹窗到输入框
                if (inputRef.current) {
                  setAtAnchorEl(inputRef.current)
                }
              } catch (err) {
                console.error('Failed to fetch users:', err)
              }
            }
          }, 500)
        }
      } else {
        setAtAnchorEl(null)
        setAtQuery('')
      }
    },
    [chatKey]
  )

  // 选择用户
  const handleSelectUser = (userid: string, nickname: string) => {
    const atIndex = inputValue.lastIndexOf('@')
    const before = inputValue.slice(0, atIndex)
    const newValue = `${before}[@id:${userid}@] `
    setInputValue(newValue)
    setAtAnchorEl(null)
    setAtQuery('')
    // 焦点返回输入框
    setTimeout(() => inputRef.current?.focus(), 0)
  }

  // 按时间正序排列消息
  const allMessages =
    data?.pages
      .flatMap(page => page.items)
      .sort((a, b) => new Date(a.create_time).getTime() - new Date(b.create_time).getTime()) || []

  // 构建 message_id -> ChatMessage 的映射，用于引用消息查找
  const messageByMsgId = useMemo(() => {
    const map = new Map<string, ChatMessage>()
    for (const msg of allMessages) {
      if (msg.message_id) {
        map.set(msg.message_id, msg)
      }
    }
    return map
  }, [allMessages])

  // 滚动到被引用的消息
  const scrollToMessage = useCallback((msgId: string) => {
    const container = containerRef.current
    if (!container) return
    const el = container.querySelector(`[data-message-id="${msgId}"]`) as HTMLElement | null
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      // 短暂高亮
      el.style.transition = 'background 0.3s'
      el.style.background = isDark ? 'rgba(56, 139, 253, 0.2)' : 'rgba(56, 139, 253, 0.12)'
      setTimeout(() => {
        el.style.background = ''
      }, 1500)
    }
  }, [isDark])

  if (isLoading) {
    return (
      <Box className="h-full flex items-center justify-center">
        <CircularProgress />
      </Box>
    )
  }

  return (
    <Box className="h-full flex flex-col overflow-hidden relative">
      {/* 消息列表容器 */}
      <Box
        ref={containerRef}
        className="flex-1 overflow-y-auto"
        sx={{
          px: { xs: 1, sm: 2 },
          py: 1,
          background: isDark
            ? 'linear-gradient(180deg, rgba(0,0,0,0.15) 0%, transparent 100%)'
            : 'linear-gradient(180deg, rgba(0,0,0,0.02) 0%, transparent 100%)',
        }}
      >
        {/* 加载更多提示 */}
        {(hasNextPage || isFetchingNextPage) && allMessages.length >= 32 && (
          <Box ref={loadMoreRef} className="p-2 flex justify-center">
            <CircularProgress size={24} />
          </Box>
        )}

        {/* 消息列表 */}
        {allMessages.length === 0 ? (
          <Box className="p-4 flex items-center justify-center h-full">
            <Typography color="textSecondary">{t('messageHistory.noMessages')}</Typography>
          </Box>
        ) : (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
            {allMessages.map((message, index) => {
              const isBot = message.sender_id === BOT_SENDER_ID && message.sender_name !== 'SYSTEM'
              const isSystem = message.sender_name === 'SYSTEM'
              const prevMsg = index > 0 ? allMessages[index - 1] : null
              const showDivider = prevMsg && needTimeDivider(prevMsg, message)
              // 同一发送者连续消息合并头像（需同时匹配 sender_id 和 sender_name，避免 SYSTEM 与 Bot 合并）
              const isContinuation =
                prevMsg &&
                !showDivider &&
                prevMsg.sender_id === message.sender_id &&
                prevMsg.sender_name === message.sender_name

              // 系统消息居中渲染
              if (isSystem) {
                return (
                  <Box key={message.id} data-message-id={message.message_id || undefined}>
                    {showDivider && (
                      <Box sx={{ display: 'flex', justifyContent: 'center', py: 1.5, my: 0.5 }}>
                        <Typography
                          variant="caption"
                          sx={{
                            color: theme.palette.text.disabled,
                            fontSize: '11px',
                            background: isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.04)',
                            px: 1.5, py: 0.3, borderRadius: '10px',
                          }}
                        >
                          {message.create_time}
                        </Typography>
                      </Box>
                    )}
                    <Box
                      sx={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        py: 1,
                        my: 0.5,
                      }}
                    >
                      <Typography
                        variant="caption"
                        sx={{
                          fontSize: '12px',
                          color: theme.palette.text.secondary,
                          fontStyle: 'italic',
                          px: 1.5,
                          py: 0.5,
                          borderRadius: '4px',
                          bgcolor: isDark ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.03)',
                          border: `1px solid ${isDark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.08)'}`,
                          maxWidth: '80%',
                        }}
                      >
                        {message.content || message.sender_nickname}
                      </Typography>
                    </Box>
                  </Box>
                )
              }

              return (
                <Box key={message.id} data-message-id={message.message_id || undefined}>
                  {/* 时间分隔线 */}
                  {showDivider && (
                    <Box
                      sx={{
                        display: 'flex',
                        justifyContent: 'center',
                        py: 1.5,
                        my: 0.5,
                      }}
                    >
                      <Typography
                        variant="caption"
                        sx={{
                          color: theme.palette.text.disabled,
                          fontSize: '11px',
                          background: isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.04)',
                          px: 1.5,
                          py: 0.3,
                          borderRadius: '10px',
                        }}
                      >
                        {message.create_time}
                      </Typography>
                    </Box>
                  )}

                  {/* 气泡布局 */}
                  <Box
                      sx={{
                        display: 'flex',
                        flexDirection: isBot ? 'row-reverse' : 'row',
                        alignItems: 'flex-start',
                        gap: 1,
                        px: 1,
                        mt: isContinuation ? 0 : 1,
                      }}
                    >
                      {/* 头像 */}
                      {isContinuation ? (
                        // 连续消息占位
                        <Box sx={{ width: 36, height: 36, flexShrink: 0 }} />
                      ) : (
                        <Avatar
                          src={getAvatarUrl(message.platform_userid)}
                          sx={{
                            width: 36,
                            height: 36,
                            flexShrink: 0,
                            fontSize: '14px',
                            fontWeight: 600,
                            bgcolor: isBot
                              ? theme.palette.primary.main
                              : nameToColor(message.sender_name),
                            mt: 0.3,
                          }}
                        >
                          {message.sender_name?.[0] ?? '?'}
                        </Avatar>
                      )}

                      {/* 内容区 */}
                      <Box
                        sx={{
                          display: 'flex',
                          flexDirection: 'column',
                          alignItems: isBot ? 'flex-end' : 'flex-start',
                          maxWidth: '75%',
                          minWidth: 0,
                        }}
                      >
                        {/* 昵称 + 时间 */}
                        {!isContinuation && (
                          <Box
                            sx={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: 0.8,
                              mb: 0.3,
                              flexDirection: isBot ? 'row-reverse' : 'row',
                            }}
                          >
                            <Typography
                              variant="caption"
                              sx={{
                                fontWeight: 600,
                                fontSize: '12px',
                                color: isBot
                                  ? theme.palette.primary.main
                                  : theme.palette.text.secondary,
                              }}
                            >
                              {message.sender_nickname || message.sender_name}
                            </Typography>
                            <Typography
                              variant="caption"
                              sx={{
                                fontSize: '11px',
                                color: theme.palette.text.disabled,
                              }}
                            >
                              {message.create_time}
                            </Typography>
                          </Box>
                        )}

                        {/* 气泡 */}
                        <Box
                          sx={{
                            position: 'relative',
                            background: isBot
                              ? isDark
                                ? 'rgba(56, 139, 253, 0.15)'
                                : 'rgba(56, 139, 253, 0.08)'
                              : isDark
                                ? 'rgba(255, 255, 255, 0.06)'
                                : 'rgba(0, 0, 0, 0.04)',
                            borderRadius: isBot
                              ? isContinuation
                                ? '12px'
                                : '12px 2px 12px 12px'
                              : isContinuation
                                ? '12px'
                                : '2px 12px 12px 12px',
                            px: 1.5,
                            py: 0.8,
                            maxWidth: '100%',
                            transition: 'background 0.15s',
                            '&:hover': {
                              background: isBot
                                ? isDark
                                  ? 'rgba(56, 139, 253, 0.22)'
                                  : 'rgba(56, 139, 253, 0.13)'
                                : isDark
                                  ? 'rgba(255, 255, 255, 0.1)'
                                  : 'rgba(0, 0, 0, 0.06)',
                            },
                          }}
                        >
                          {/* 引用消息 - QQ风格内嵌气泡 */}
                          {aiAlwaysIncludeMsgId && message.ref_msg_id && (() => {
                            const refMsg = messageByMsgId.get(message.ref_msg_id)
                            return (
                              <Box
                                onClick={() => refMsg ? scrollToMessage(message.ref_msg_id!) : undefined}
                                sx={{
                                  display: 'flex',
                                  alignItems: 'center',
                                  gap: 0.5,
                                  width: '100%',
                                  mb: 0.6,
                                  pl: 1,
                                  pr: 0.5,
                                  py: 0.5,
                                  borderLeft: `2px solid ${theme.palette.primary.main}`,
                                  borderRadius: '2px',
                                  bgcolor: isDark ? 'rgba(0,0,0,0.15)' : 'rgba(0,0,0,0.04)',
                                  cursor: refMsg ? 'pointer' : 'default',
                                  transition: 'background 0.15s',
                                  boxSizing: 'border-box',
                                  overflow: 'hidden',
                                  '&:hover': refMsg ? {
                                    bgcolor: isDark ? 'rgba(0,0,0,0.25)' : 'rgba(0,0,0,0.07)',
                                  } : {},
                                }}
                              >
                                <ReplyIcon sx={{ fontSize: 13, color: theme.palette.text.disabled, transform: 'scaleX(-1)', flexShrink: 0 }} />
                                {refMsg ? (
                                  <Typography
                                    component="div"
                                    variant="caption"
                                    sx={{
                                      fontSize: '11px',
                                      color: theme.palette.text.secondary,
                                      overflow: 'hidden',
                                      textOverflow: 'ellipsis',
                                      whiteSpace: 'nowrap',
                                      lineHeight: 1.4,
                                      minWidth: 0,
                                    }}
                                  >
                                    <Box component="span" sx={{ fontWeight: 600, fontSize: '11px', color: theme.palette.text.primary, mr: 0.5 }}>
                                      {refMsg.sender_nickname || refMsg.sender_name}
                                    </Box>
                                    {(refMsg.content_data && refMsg.content_data.length > 0)
                                      ? refMsg.content_data.map((seg, idx) => {
                                          if (seg.type === 'at') {
                                            return (
                                              <Box
                                                key={idx}
                                                component="span"
                                                sx={{
                                                  color: theme.palette.primary.main,
                                                  fontWeight: 600,
                                                }}
                                              >
                                                @{seg.target_nickname || 'User'}
                                              </Box>
                                            )
                                          }
                                          if (seg.type === 'text') {
                                            return <span key={idx}>{seg.text}</span>
                                          }
                                          if (seg.type === 'image') {
                                            return <span key={idx} style={{ color: theme.palette.text.disabled }}>[图片]</span>
                                          }
                                          if (seg.type === 'file' || seg.type === 'voice' || seg.type === 'video') {
                                            return <span key={idx} style={{ color: theme.palette.text.disabled }}>[{seg.type === 'voice' ? '语音' : seg.type === 'video' ? '视频' : '文件'}]</span>
                                          }
                                          return null
                                        })
                                      : (refMsg.content || '...')
                                    }
                                  </Typography>
                                ) : (
                                  <Typography
                                    variant="caption"
                                    sx={{
                                      fontSize: '11px',
                                      color: theme.palette.text.disabled,
                                      fontStyle: 'italic',
                                    }}
                                  >
                                    {t('messageHistory.quotedMessage')}
                                  </Typography>
                                )}
                              </Box>
                            )
                          })()}
                          <MessageContent
                            message={message}
                            noContentText={t('messageHistory.noContent')}
                          />
                        </Box>
                      </Box>
                    </Box>
                </Box>
              )
            })}
          </Box>
        )}
        <div ref={messagesEndRef} />
      </Box>

      {/* 回到底部按钮 */}
      {!autoScroll && (
        <Box
          sx={{
            position: 'absolute',
            bottom: canSend ? 72 : 16,
            right: 16,
            zIndex: theme.zIndex.fab,
          }}
        >
          <Button
            variant="contained"
            color="primary"
            size="small"
            onClick={handleScrollToBottom}
            sx={{
              minWidth: 'auto',
              borderRadius: 20,
              boxShadow: theme.shadows[6],
            }}
          >
            {t('messageHistory.scrollToBottom')}
          </Button>
        </Box>
      )}

      {/* 附件预览 */}
      {canSend && attachedFile && (
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            gap: 1,
            px: 2,
            py: 0.5,
            borderTop: `1px solid ${theme.palette.divider}`,
            bgcolor: isDark ? 'rgba(0,0,0,0.15)' : 'rgba(0,0,0,0.01)',
            fontSize: '12px',
            color: theme.palette.text.secondary,
          }}
        >
          {attachedFile.type.startsWith('image/') && filePreviewUrl ? (
            <img
              src={filePreviewUrl}
              alt="preview"
              style={{ height: 40, borderRadius: 4, objectFit: 'cover' }}
            />
          ) : (
            <span>📎</span>
          )}
          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {attachedFile.name}
          </span>
          <IconButton size="small" onClick={() => setAttachedFile(null)}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Box>
      )}

      {/* 输入框 */}
      {canSend && (
      <>
      {/* 发送身份选择 */}
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 1,
          px: 1.5,
          pt: 0.75,
          pb: 0,
          borderTop: attachedFile ? 'none' : `1px solid ${theme.palette.divider}`,
          bgcolor: isDark ? 'rgba(0,0,0,0.2)' : 'rgba(0,0,0,0.02)',
        }}
      >
        <Typography variant="caption" sx={{ color: theme.palette.text.secondary, whiteSpace: 'nowrap' }}>
          {t('messageHistory.senderType')}
        </Typography>
        <ToggleButtonGroup
          value={senderType}
          exclusive
          size="small"
          onChange={(_, val) => { if (val) setSenderType(val) }}
          sx={{
            '& .MuiToggleButton-root': {
              py: 0,
              px: 1,
              fontSize: '12px',
              textTransform: 'none',
              lineHeight: '24px',
            },
          }}
        >
          <ToggleButton value="bot">{t('messageHistory.senderBot')}</ToggleButton>
          <ToggleButton value="system">{t('messageHistory.senderSystem')}</ToggleButton>
          <ToggleButton value="none">{t('messageHistory.senderNone')}</ToggleButton>
        </ToggleButtonGroup>
        <Typography variant="caption" sx={{
          color: senderType === 'none' ? theme.palette.warning.main : theme.palette.text.secondary,
          fontSize: '11px',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          minWidth: 0,
        }}>
          {senderType === 'bot' && t('messageHistory.senderBotHint')}
          {senderType === 'system' && t('messageHistory.senderSystemHint')}
          {senderType === 'none' && t('messageHistory.senderNoneHint')}
        </Typography>
      </Box>
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 0.5,
          px: 1.5,
          py: 1,
          bgcolor: isDark ? 'rgba(0,0,0,0.2)' : 'rgba(0,0,0,0.02)',
        }}
      >
        {/* 隐藏的文件输入 */}
        <input
          ref={fileInputRef}
          type="file"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) setAttachedFile(f)
            e.target.value = ''
          }}
        />
        <IconButton
          size="small"
          onClick={() => fileInputRef.current?.click()}
          disabled={sending}
          sx={{ color: theme.palette.text.secondary }}
        >
          <AttachFileIcon fontSize="small" />
        </IconButton>
        <TextField
          ref={inputRef}
          fullWidth
          size="small"
          multiline
          maxRows={3}
          placeholder={t('messageHistory.inputPlaceholder')}
          value={inputValue}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          onCompositionStart={() => { isComposingRef.current = true }}
          onCompositionEnd={(e) => {
            isComposingRef.current = false
            // 组合结束后，用最终值重新触发一次 @mention 检测
            handleInputChange(e as unknown as React.ChangeEvent<HTMLInputElement>)
          }}
          disabled={sending}
          sx={{
            '& .MuiOutlinedInput-root': {
              borderRadius: '20px',
              fontSize: '13.5px',
            },
          }}
        />
        <IconButton
          color="primary"
          onClick={handleSend}
          disabled={(!inputValue.trim() && !attachedFile) || sending}
          sx={{
            width: 36,
            height: 36,
          }}
        >
          {sending ? <CircularProgress size={20} /> : <SendIcon fontSize="small" />}
        </IconButton>
      </Box>

      {/* 输入框渲染预览 - 仅在包含 @mention 标记时显示 */}
      <Box
        sx={{
          display: inputValue && /\[@id:\d+@\]/.test(inputValue) ? 'flex' : 'none',
          flexWrap: 'wrap',
          alignItems: 'center',
          gap: 0.5,
          px: 2,
          py: 0.5,
          fontSize: '12px',
          lineHeight: 1.6,
          color: theme.palette.text.secondary,
          borderTop: `1px solid ${theme.palette.divider}`,
        }}
      >
          {(() => {
            const parts: Array<{ type: 'text' | 'mention'; content: string; id?: string; nickname?: string }> = []
            const mentionPattern = /\[@id:(\d+)@\]/g
            let lastIndex = 0
            let match

            while ((match = mentionPattern.exec(inputValue)) !== null) {
              if (match.index > lastIndex) {
                parts.push({
                  type: 'text',
                  content: inputValue.slice(lastIndex, match.index),
                })
              }
              const userId = match[1]
              const user = atUsers.find(u => u.platform_userid === userId)
              parts.push({
                type: 'mention',
                content: match[0],
                id: userId,
                nickname: user?.nickname || `User_${userId}`,
              })
              lastIndex = mentionPattern.lastIndex
            }

            if (lastIndex < inputValue.length) {
              parts.push({
                type: 'text',
                content: inputValue.slice(lastIndex),
              })
            }

            return parts
          })().map((part, idx) =>
            part.type === 'text' ? (
              <span key={idx}>{part.content}</span>
            ) : (
              <Chip
                key={idx}
                label={`@${part.nickname}`}
                size="small"
                variant="outlined"
                sx={{
                  fontWeight: 600,
                  fontSize: '11px',
                  height: 22,
                  bgcolor: isDark ? 'rgba(33, 150, 243, 0.15)' : 'rgba(33, 150, 243, 0.1)',
                  borderColor: theme.palette.primary.main,
                  color: theme.palette.primary.main,
                }}
              />
            )
          )}
        </Box>

      </>
      )}

      {/* @ 用户列表弹窗 */}
      <Popover
        open={Boolean(atAnchorEl)}
        anchorEl={atAnchorEl}
        onClose={() => setAtAnchorEl(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'left' }}
        transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        PaperProps={{
          sx: {
            maxHeight: 300,
            width: 280,
            borderRadius: 1,
          },
        }}
      >
        <List sx={{ py: 0 }}>
          {atUsers
            .filter(u => u.nickname.startsWith(atQuery) || u.platform_userid.startsWith(atQuery))
            .map(user => (
              <ListItemButton
                key={user.platform_userid}
                onClick={() => handleSelectUser(user.platform_userid, user.nickname)}
                sx={{
                  py: 1,
                  px: 1.5,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 1,
                  '&:hover': {
                    bgcolor: theme.palette.action.hover,
                  },
                }}
              >
                <Avatar
                  src={getAvatarUrl(user.platform_userid)}
                  sx={{
                    width: 32,
                    height: 32,
                    flexShrink: 0,
                    fontSize: '12px',
                    bgcolor: nameToColor(user.nickname),
                  }}
                >
                  {user.nickname?.[0] ?? '?'}
                </Avatar>
                <Box sx={{ flex: 1, minWidth: 0 }}>
                  <Typography variant="body2" sx={{ fontSize: '13px', fontWeight: 500 }}>
                    {user.nickname}
                  </Typography>
                  <Typography variant="caption" sx={{ fontSize: '11px', color: theme.palette.text.disabled }}>
                    {user.platform_userid}
                  </Typography>
                </Box>
              </ListItemButton>
            ))}
          {atUsers.filter(u => u.nickname.startsWith(atQuery) || u.platform_userid.startsWith(atQuery)).length === 0 && (
            <ListItem>
              <Typography variant="caption" sx={{ width: '100%', textAlign: 'center', py: 1 }}>
                没有找到用户
              </Typography>
            </ListItem>
          )}
        </List>
      </Popover>

      {/* 提示 */}
      <Snackbar
        open={snack.open}
        autoHideDuration={3000}
        onClose={() => setSnack(s => ({ ...s, open: false }))}
        anchorOrigin={{ vertical: 'top', horizontal: 'center' }}
      >
        <Alert
          severity={snack.severity}
          onClose={() => setSnack(s => ({ ...s, open: false }))}
          variant="filled"
          sx={{ width: '100%' }}
        >
          {snack.message}
        </Alert>
      </Snackbar>
    </Box>
  )
}
