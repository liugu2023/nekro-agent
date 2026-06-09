import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Autocomplete,
  Box,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  FormControl,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  SelectChangeEvent,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import Grid from '@mui/material/Grid2'
import {
  Add as AddIcon,
  AutoAwesome as AutoAwesomeIcon,
  Code as CodeIcon,
  History as HistoryIcon,
  PlayArrow as PlayArrowIcon,
  Refresh as RefreshIcon,
  Restore as RestoreIcon,
  Save as SaveIcon,
  Send as SendIcon,
  Settings as SettingsIcon,
  SmartToy as SmartToyIcon,
  Stop as StopIcon,
  Terminal as TerminalIcon,
} from '@mui/icons-material'
import { alpha, useTheme } from '@mui/material/styles'
import { Editor } from '@monaco-editor/react'
import { useTranslation } from 'react-i18next'

import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import { ccModelPresetApi, CCModelPresetInfo } from '../../services/api/cc-model-preset'
import {
  pluginDevApi,
  PluginDevHistoryResponse,
  PluginDevStatusResponse,
  PluginDevTaskResponse,
  PluginDevTaskStatus,
  PluginDevVersionInfo,
  streamPluginDevTask,
} from '../../services/api/plugin-dev'
import { pluginEditorApi } from '../../services/api/plugin-editor'
import { useNotification } from '../../hooks/useNotification'
import { BORDER_RADIUS, CARD_STYLES, CHIP_VARIANTS } from '../../theme/variants'

const PLUGIN_CC_EDITOR_DRAFT_KEY = 'nekro-plugin-cc-editor-draft'
const RUNNING_TASK_STATUSES = new Set<PluginDevTaskStatus>(['pending', 'running_cc', 'creating_proposal'])

type NewPluginCreateMode = 'file' | 'folder'
type RollbackTarget = 'before' | 'after'

type Translate = ReturnType<typeof useTranslation>['t']

interface PluginCcEditorDraft {
  selectedFile: string
  code: string
  originalCode: string
  prompt: string
  generatedCode: string
  taskId: string
}

interface StatusChipProps {
  label: string
  tone: 'success' | 'warning' | 'error' | 'info' | 'neutral' | 'primary'
  icon?: React.ReactElement
}

function StatusChip({ label, tone, icon }: StatusChipProps) {
  const theme = useTheme()
  const colorMap = {
    success: theme.palette.success.main,
    warning: theme.palette.warning.main,
    error: theme.palette.error.main,
    info: theme.palette.info.main,
    neutral: theme.palette.text.secondary,
    primary: theme.palette.primary.main,
  }

  return (
    <Chip
      icon={icon}
      size="small"
      label={label}
      variant="outlined"
      sx={CHIP_VARIANTS.getCustomColorChip(colorMap[tone], true)}
    />
  )
}

const getTaskTone = (status?: PluginDevTaskStatus): StatusChipProps['tone'] => {
  if (!status) return 'neutral'
  if (status === 'waiting_apply' || status === 'applied') return 'success'
  if (status === 'failed') return 'error'
  if (status === 'cancelled') return 'warning'
  return 'info'
}

const getVersionChannelLabel = (version: PluginDevVersionInfo, t: Translate): string => (
  t(`editor.pluginDev.versionChannels.${version.nekro_agent_channel}`)
)

const getSourceOriginLabel = (version: PluginDevVersionInfo, t: Translate): string => (
  t(`editor.pluginDev.sourceOrigins.${version.source_origin}`)
)

const safeParseDraft = (raw: string | null): Partial<PluginCcEditorDraft> | null => {
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as Partial<PluginCcEditorDraft>
    return typeof parsed === 'object' && parsed !== null ? parsed : null
  } catch {
    window.localStorage.removeItem(PLUGIN_CC_EDITOR_DRAFT_KEY)
    return null
  }
}

interface DiffViewerProps {
  diff: string
}

function DiffViewer({ diff }: DiffViewerProps) {
  const theme = useTheme()
  const lines = diff
    .split('\n')
    .filter(line => !line.startsWith('@@') && !line.startsWith('+++') && !line.startsWith('---'))

  const getLineStyle = (line: string) => {
    if (line.startsWith('+')) {
      return {
        backgroundColor: alpha(theme.palette.success.main, 0.14),
        color: theme.palette.success.dark,
      }
    }
    if (line.startsWith('-')) {
      return {
        backgroundColor: alpha(theme.palette.error.main, 0.12),
        color: theme.palette.error.dark,
      }
    }
    return {
      color: theme.palette.text.secondary,
    }
  }

  return (
    <Box sx={{ height: '100%', overflow: 'auto', backgroundColor: 'background.paper' }}>
      {lines.map((line, index) => (
        <Box
          key={`${index}-${line}`}
          component="pre"
          sx={{
            m: 0,
            px: 1.25,
            py: 0.25,
            minHeight: 22,
            fontFamily: 'Consolas, Monaco, monospace',
            fontSize: 13,
            lineHeight: 1.55,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            borderLeft: line.startsWith('+') && !line.startsWith('+++')
              ? `3px solid ${theme.palette.success.main}`
              : line.startsWith('-') && !line.startsWith('---')
                ? `3px solid ${theme.palette.error.main}`
                : '3px solid transparent',
            ...getLineStyle(line),
          }}
        >
          {line || ' '}
        </Box>
      ))}
    </Box>
  )
}

interface ClaudeCodeLogEntry {
  raw: string
  kind: 'toolCall' | 'toolResult' | 'error' | 'status'
  tool: string
  target: string
  text: string
  detailText: string
  payload: ToolLogPayload | null
}

interface ToolLogPayload {
  name: string
  toolUseId: string
  input: Record<string, unknown>
  description: string
  primaryKey: string
  primaryVal: string
  content: string
  isError: boolean
}

const TOOL_PRIMARY_KEYS = ['command', 'file_path', 'pattern', 'url', 'query', 'prompt', 'notebook_path', 'path']

const asRecord = (value: unknown): Record<string, unknown> => {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return {}
  return value as Record<string, unknown>
}

const stringifyToolValue = (value: unknown): string => {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

const getStringField = (record: Record<string, unknown>, key: string): string => {
  const value = record[key]
  return typeof value === 'string' ? value : ''
}

const pickPrimaryToolValue = (input: Record<string, unknown>): { key: string; value: string } => {
  for (const key of TOOL_PRIMARY_KEYS) {
    const value = getStringField(input, key).trim()
    if (value) return { key, value }
  }

  for (const [key, value] of Object.entries(input)) {
    if (key === 'description') continue
    const text = stringifyToolValue(value).trim()
    if (text) return { key, value: text }
  }
  return { key: '', value: '' }
}

const parseToolPayload = (rawPayload: string): ToolLogPayload | null => {
  if (!rawPayload.startsWith('{')) return null
  try {
    const parsed = asRecord(JSON.parse(rawPayload))
    const input = asRecord(parsed.input)
    const primary = pickPrimaryToolValue(input)
    return {
      name: getStringField(parsed, 'name') || 'unknown',
      toolUseId: getStringField(parsed, 'tool_use_id') || getStringField(parsed, 'toolUseId'),
      input,
      description: getStringField(parsed, 'description') || getStringField(input, 'description'),
      primaryKey: getStringField(parsed, 'primary_key') || getStringField(parsed, 'primaryKey') || primary.key,
      primaryVal: getStringField(parsed, 'primary_value') || getStringField(parsed, 'primaryVal') || primary.value,
      content: stringifyToolValue(parsed.content).trim(),
      isError: parsed.is_error === true || parsed.isError === true,
    }
  } catch {
    return null
  }
}

const parseClaudeCodeLogEntry = (raw: string): ClaudeCodeLogEntry => {
  const trimmed = raw.trim()
  const parseTool = (prefix: string, kind: ClaudeCodeLogEntry['kind']): ClaudeCodeLogEntry | null => {
    if (!trimmed.startsWith(prefix)) return null
    const rest = trimmed.slice(prefix.length).trim()
    const payload = parseToolPayload(rest)
    if (payload) {
      return {
        raw,
        kind,
        tool: payload.name,
        target: payload.primaryVal,
        text: rest,
        detailText: '',
        payload,
      }
    }

    const lineBreakIndex = rest.indexOf('\n')
    const header = lineBreakIndex >= 0 ? rest.slice(0, lineBreakIndex).trim() : rest
    const detailText = lineBreakIndex >= 0 ? rest.slice(lineBreakIndex + 1).trim() : ''
    const [tool = 'unknown', ...targetParts] = header.split(/\s+/)
    return {
      raw,
      kind,
      tool,
      target: targetParts.join(' '),
      text: rest,
      detailText,
      payload: null,
    }
  }

  const toolCall = parseTool('工具调用：', 'toolCall')
  if (toolCall) return toolCall
  const toolResult = parseTool('工具结果：', 'toolResult')
  if (toolResult) return toolResult

  return {
    raw,
    kind: trimmed.includes('失败') || trimmed.includes('错误') ? 'error' : 'status',
    tool: '',
    target: '',
    text: trimmed,
    detailText: '',
    payload: null,
  }
}

function ClaudeCodeFlowLog({ logs }: { logs: string[] }) {
  const theme = useTheme()
  const entries = logs
    .map(parseClaudeCodeLogEntry)
    .filter(entry => {
      if (entry.kind !== 'toolResult') return true
      if (!entry.payload) return Boolean(entry.target || entry.detailText)
      return entry.payload.isError || Boolean(entry.payload.content.trim())
    })

  const renderDetailBlock = (label: string, value: string, tone: 'default' | 'error' = 'default') => {
    if (!value.trim()) return null
    const detailColor = tone === 'error' ? theme.palette.error.main : theme.palette.text.secondary
    return (
      <Box sx={{ mt: 0.35 }}>
        <Typography
          component="div"
          sx={{
            color: detailColor,
            fontFamily: 'Consolas, Monaco, monospace',
            fontSize: 11,
            fontWeight: 700,
            lineHeight: 1.45,
          }}
        >
          {label}
        </Typography>
        <Box
          component="pre"
          sx={{
            m: 0,
            mt: 0.15,
            px: 0.75,
            py: 0.5,
            overflowX: 'auto',
            color: tone === 'error' ? theme.palette.error.main : theme.palette.text.primary,
            backgroundColor: alpha(tone === 'error' ? theme.palette.error.main : theme.palette.text.primary, 0.055),
            borderLeft: `2px solid ${alpha(tone === 'error' ? theme.palette.error.main : theme.palette.primary.main, 0.45)}`,
            borderRadius: 0.75,
            fontFamily: 'Consolas, Monaco, monospace',
            fontSize: 11,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {value}
        </Box>
      </Box>
    )
  }

  const renderToolDetails = (entry: ClaudeCodeLogEntry) => {
    if (!entry.payload) {
      return entry.detailText ? renderDetailBlock('详情', entry.detailText) : null
    }

    const { input, content, description, isError } = entry.payload
    const toolName = entry.tool.toLowerCase()
    const command = getStringField(input, 'command')
    const cwd = getStringField(input, 'cwd') || getStringField(input, 'workdir')
    const timeout = input.timeout ?? input.timeout_ms
    const isBash = toolName === 'bash'

    if (entry.kind === 'toolResult') {
      if (!content && !isError) return null
      return (
        <Stack spacing={0.35} sx={{ mt: 0.25 }}>
          {isError ? (
            <Typography
              component="div"
              sx={{
                color: theme.palette.error.main,
                fontFamily: 'Consolas, Monaco, monospace',
                fontSize: 11,
                fontWeight: 700,
              }}
            >
              状态：失败
            </Typography>
          ) : null}
          {renderDetailBlock(isError ? '错误输出' : '输出', content, isError ? 'error' : 'default')}
        </Stack>
      )
    }

    if (isBash) {
      const extraInput = Object.fromEntries(
        Object.entries(input).filter(([key]) => !['command', 'description', 'cwd', 'workdir', 'timeout', 'timeout_ms'].includes(key))
      )
      return (
        <Stack spacing={0.35} sx={{ mt: 0.25 }}>
          {description ? (
            <Typography component="div" sx={{ color: theme.palette.text.secondary, fontFamily: 'Consolas, Monaco, monospace', fontSize: 11, lineHeight: 1.45 }}>
              说明：{description}
            </Typography>
          ) : null}
          {cwd ? (
            <Typography component="div" sx={{ color: theme.palette.text.secondary, fontFamily: 'Consolas, Monaco, monospace', fontSize: 11, lineHeight: 1.45 }}>
              工作目录：{cwd}
            </Typography>
          ) : null}
          {timeout !== undefined ? (
            <Typography component="div" sx={{ color: theme.palette.text.secondary, fontFamily: 'Consolas, Monaco, monospace', fontSize: 11, lineHeight: 1.45 }}>
              超时：{stringifyToolValue(timeout)}
            </Typography>
          ) : null}
          {renderDetailBlock('命令', command)}
          {Object.keys(extraInput).length ? renderDetailBlock('参数', stringifyToolValue(extraInput)) : null}
        </Stack>
      )
    }

    return Object.keys(input).length
      ? renderDetailBlock('参数', stringifyToolValue(input))
      : null
  }

  return (
    <Stack
      spacing={0.45}
      sx={{
        mt: 0.25,
        pl: 0.25,
      }}
    >
        {entries.map((entry, index) => {
          const isToolCall = entry.kind === 'toolCall'
          const isToolResult = entry.kind === 'toolResult'
          const icon = isToolCall ? '⏺' : isToolResult ? '⎿' : '·'
          const showTarget = isToolCall && entry.tool.toLowerCase() !== 'bash' && Boolean(entry.target)
          const color = entry.kind === 'error'
            ? theme.palette.error.main
            : isToolCall || isToolResult
              ? theme.palette.text.primary
              : theme.palette.text.disabled

          return (
            <Box
              key={`${index}-${entry.raw}`}
              sx={{
                display: 'grid',
                gridTemplateColumns: '18px minmax(0, 1fr)',
                columnGap: 0.75,
                alignItems: 'start',
                color,
                fontFamily: 'Consolas, Monaco, monospace',
                fontSize: 12,
                lineHeight: 1.45,
              }}
            >
              <Box component="span" sx={{ color: isToolCall ? theme.palette.primary.main : isToolResult ? theme.palette.text.secondary : 'inherit', textAlign: 'center' }}>
                {icon}
              </Box>
              <Box sx={{ minWidth: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                {isToolCall || isToolResult
                  ? (
                    <>
                      <Box component="span" sx={{ fontWeight: isToolCall ? 700 : 500 }}>{entry.tool}</Box>
                      {showTarget ? (
                        <Box component="span" sx={{ color: theme.palette.text.secondary }}> {entry.target}</Box>
                      ) : null}
                      {renderToolDetails(entry)}
                    </>
                  )
                  : entry.text}
              </Box>
            </Box>
          )
        })}
    </Stack>
  )
}

interface TaskStatusHeaderProps {
  status: PluginDevStatusResponse | null
  task: PluginDevTaskResponse | null
  selectedFile: string
  isGenerating: boolean
  isApplyingProposal: boolean
  canGenerate: boolean
  canOpenHistory: boolean
  canStopTask: boolean
  canReconnectTaskStream: boolean
  onOpenConfig: () => void
  onOpenHistory: () => void
  onStartSandbox: () => void
  onStopTask: () => void
  onReconnectTaskStream: () => void
  onGenerate: () => void
  t: Translate
}

function TaskStatusHeader({
  status,
  task,
  selectedFile,
  isGenerating,
  isApplyingProposal,
  canGenerate,
  canOpenHistory,
  canStopTask,
  canReconnectTaskStream,
  onOpenConfig,
  onOpenHistory,
  onStartSandbox,
  onStopTask,
  onReconnectTaskStream,
  onGenerate,
  t,
}: TaskStatusHeaderProps) {
  const sandboxRunning = status?.sandbox_status === 'running'
  const version = status?.version

  return (
    <Paper sx={{ ...CARD_STYLES.DEFAULT, p: 2 }}>
      <Stack spacing={1.5}>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} alignItems={{ xs: 'stretch', md: 'center' }}>
          <Box sx={{ flexGrow: 1, minWidth: 0 }}>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5, flexWrap: 'wrap' }}>
              <Typography variant="h6" sx={{ fontWeight: 800 }}>
                {t('editor.pluginDev.pageTitle')}
              </Typography>
              <StatusChip
                icon={<TerminalIcon />}
                label={status?.sandbox_status || t('editor.pluginDev.status.unknown')}
                tone={sandboxRunning ? 'success' : status?.sandbox_status === 'failed' ? 'error' : 'neutral'}
              />
              <StatusChip
                label={task?.status || (isGenerating ? 'pending' : t('editor.pluginDev.status.idle'))}
                tone={getTaskTone(task?.status ?? (isGenerating ? 'pending' : undefined))}
              />
              {status?.queue_length ? (
                <StatusChip label={`${t('editor.pluginDev.queue')}: ${status.queue_length}`} tone="warning" />
              ) : null}
            </Stack>
            <Typography variant="body2" color="text.secondary" noWrap>
              {selectedFile || t('editor.pluginDev.noFileSelected')} · {status?.cc_model_preset_name || t('editor.pluginDev.defaultPreset')}
              {version ? ` · ${t('editor.pluginDev.versionFields.channel')}: ${getVersionChannelLabel(version, t)}` : ''}
            </Typography>
          </Box>
          <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', justifyContent: { xs: 'flex-start', md: 'flex-end' } }}>
            <ActionButton startIcon={<SettingsIcon />} onClick={onOpenConfig} disabled={isGenerating || isApplyingProposal}>
              {t('editor.pluginDev.config')}
            </ActionButton>
            <ActionButton
              startIcon={<HistoryIcon />}
              onClick={onOpenHistory}
              disabled={!canOpenHistory || isApplyingProposal}
            >
              {t('editor.pluginDev.history')}
            </ActionButton>
            <ActionButton
              startIcon={<PlayArrowIcon />}
              onClick={onStartSandbox}
              disabled={sandboxRunning || isGenerating || isApplyingProposal}
            >
              {t('editor.pluginDev.startSandbox')}
            </ActionButton>
            <ActionButton
              startIcon={<RefreshIcon />}
              onClick={onReconnectTaskStream}
              disabled={!canReconnectTaskStream || isApplyingProposal}
            >
              {t('editor.pluginDev.reconnectLogs')}
            </ActionButton>
            <ActionButton
              tone="danger"
              startIcon={<StopIcon />}
              onClick={onStopTask}
              disabled={!canStopTask || isApplyingProposal}
            >
              {t('editor.pluginDev.stopTask')}
            </ActionButton>
            <ActionButton
              tone="primary"
              startIcon={isGenerating ? <CircularProgress size={16} color="inherit" /> : <AutoAwesomeIcon />}
              onClick={onGenerate}
              disabled={!canGenerate || isApplyingProposal}
            >
              {isGenerating ? t('editor.pluginDev.generating') : t('editor.pluginDev.generate')}
            </ActionButton>
          </Stack>
        </Stack>
      </Stack>
    </Paper>
  )
}

interface EditorContextPanelProps {
  files: string[]
  selectedFile: string
  code: string
  hasLocalChanges: boolean
  isBusy: boolean
  onFileSelect: (event: SelectChangeEvent<string>) => void
  onOpenCreatePlugin: () => void
  onCodeChange: (value: string) => void
  t: Translate
}

function EditorContextPanel({
  files,
  selectedFile,
  code,
  hasLocalChanges,
  isBusy,
  onFileSelect,
  onOpenCreatePlugin,
  onCodeChange,
  t,
}: EditorContextPanelProps) {
  const theme = useTheme()

  return (
    <Paper sx={{ ...CARD_STYLES.DEFAULT, p: 2, width: '100%', height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0, gap: 1.5 }}>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ flexShrink: 0 }}>
        <FormControl fullWidth size="small">
          <InputLabel>{t('editor.selectPluginFile')}</InputLabel>
          <Select value={selectedFile} label={t('editor.selectPluginFile')} onChange={onFileSelect} disabled={isBusy}>
            {files.map(file => <MenuItem key={file} value={file}>{file}</MenuItem>)}
          </Select>
        </FormControl>
        <ActionButton startIcon={<AddIcon />} onClick={onOpenCreatePlugin} disabled={isBusy} sx={{ minWidth: 110 }}>
          {t('editor.create')}
        </ActionButton>
      </Stack>
      {hasLocalChanges && <Alert severity="warning" sx={{ flexShrink: 0 }}>{t('editor.pluginDev.unsavedContext')}</Alert>}
      <Stack direction="row" spacing={1} alignItems="center" sx={{ flexShrink: 0 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexGrow: 1, minWidth: 0 }}>
          <CodeIcon fontSize="small" color="primary" />
          <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>{t('editor.pluginDev.sourceSnapshot')}</Typography>
        </Box>
      </Stack>
      <Box sx={{ flex: 1, minHeight: 0, border: 1, borderColor: 'divider', borderRadius: BORDER_RADIUS.DEFAULT, overflow: 'hidden' }}>
        <Editor
          height="100%"
          defaultLanguage="python"
          theme={theme.palette.mode === 'dark' ? 'vs-dark' : 'light'}
          value={code}
          onChange={value => onCodeChange(value || '')}
          options={{ minimap: { enabled: false }, fontSize: 13, tabSize: 4, automaticLayout: true, formatOnPaste: true }}
        />
      </Box>
    </Paper>
  )
}

interface CcConversationPanelProps {
  prompt: string
  task: PluginDevTaskResponse | null
  status: PluginDevStatusResponse | null
  generatedCode: string
  isGenerating: boolean
  isApplyingProposal: boolean
  proposalReady: boolean
  canApplyProposal: boolean
  onPromptChange: (value: string) => void
  onSend: () => void
  onClearProposal: () => void
  onApplyToEditor: () => void
  onApplyProposal: () => void
  t: Translate
}

function CcConversationPanel({
  prompt,
  task,
  status,
  generatedCode,
  isGenerating,
  isApplyingProposal,
  proposalReady,
  canApplyProposal,
  onPromptChange,
  onSend,
  onClearProposal,
  onApplyToEditor,
  onApplyProposal,
  t,
}: CcConversationPanelProps) {
  const theme = useTheme()
  const composerRef = useRef(false)
  const messageScrollRef = useRef<HTMLDivElement | null>(null)
  const canSend = Boolean(prompt.trim()) && !isGenerating && !isApplyingProposal && !proposalReady
  const hasProposalContent = Boolean(proposalReady && (task?.diff || task?.result_code || generatedCode))
  const flowLogs = (task?.logs ?? [])
    .filter(log => log.trim() && !log.startsWith('CC 已返回约'))

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing || composerRef.current) return
    event.preventDefault()
    if (canSend) onSend()
  }

  useEffect(() => {
    const element = messageScrollRef.current
    if (!element) return
    element.scrollTop = element.scrollHeight
  }, [generatedCode, isGenerating, task?.status, task?.diff, task?.result_code, task?.summary])

  return (
    <Paper sx={{ ...CARD_STYLES.DEFAULT, p: 0, width: '100%', height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <Stack ref={messageScrollRef} spacing={1.5} sx={{ flex: 1, minHeight: 0, overflow: 'auto', p: 2 }}>
        <Stack direction="row" spacing={1.25} alignItems="flex-start">
          <Box sx={{ width: 32, height: 32, borderRadius: BORDER_RADIUS.DEFAULT, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'primary.main', backgroundColor: alpha(theme.palette.primary.main, 0.12), flexShrink: 0 }}>
            <SmartToyIcon fontSize="small" />
          </Box>
          <Paper variant="outlined" sx={{ p: 1.25, maxWidth: '88%', backgroundColor: 'background.paper' }}>
            <Typography variant="body2">{t('editor.pluginDev.conversationIntro')}</Typography>
            <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
              {status?.version
                ? [
                    `${t('editor.pluginDev.versionFields.channel')}: ${getVersionChannelLabel(status.version, t)}`,
                    `${t('editor.pluginDev.versionFields.source')}: ${getSourceOriginLabel(status.version, t)}${status.version.source_dirty ? ` · ${t('editor.pluginDev.versionFields.dirty')}` : ''}`,
                  ].join(' · ')
                : t('editor.pluginDev.noVersionNotes')}
            </Typography>
          </Paper>
        </Stack>

        {task?.summary && (
          <Stack direction="row" spacing={1.25} justifyContent="flex-end">
            <Paper sx={{ p: 1.25, maxWidth: '88%', color: 'primary.contrastText', backgroundColor: 'primary.main' }}>
              <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{task.summary}</Typography>
            </Paper>
          </Stack>
        )}

        {(task?.status === 'failed' || task?.status === 'applied') && (
          <Stack direction="row" spacing={1.25} alignItems="flex-start">
            <Box sx={{ width: 32, height: 32, borderRadius: BORDER_RADIUS.DEFAULT, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'primary.main', backgroundColor: alpha(theme.palette.primary.main, 0.12), flexShrink: 0 }}>
              <SmartToyIcon fontSize="small" />
            </Box>
            <Paper variant="outlined" sx={{ p: 1.25, maxWidth: '88%', backgroundColor: 'background.paper' }}>
              <Typography variant="body2" sx={{ fontWeight: 700 }}>{t('editor.pluginDev.assistantName')}</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                {task?.status === 'failed'
                  ? task.error || t('editor.messages.pluginDevGenerateFailed')
                  : t('editor.pluginDev.appliedState')}
              </Typography>
            </Paper>
          </Stack>
        )}

        {flowLogs.length ? (
          <Box sx={{ width: '100%' }}>
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ display: 'block', mb: 0.5, fontWeight: 700, letterSpacing: 0 }}
            >
              {t('editor.pluginDev.flowTitle')}
            </Typography>
            <ClaudeCodeFlowLog logs={flowLogs} />
          </Box>
        ) : null}

        {hasProposalContent && (
          <Stack direction="row" spacing={1.25} alignItems="flex-start">
            <Box sx={{ width: 32, height: 32, borderRadius: BORDER_RADIUS.DEFAULT, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'primary.main', backgroundColor: alpha(theme.palette.primary.main, 0.12), flexShrink: 0 }}>
              <SmartToyIcon fontSize="small" />
            </Box>
            <Paper variant="outlined" sx={{ overflow: 'hidden', width: 'min(100%, 780px)', maxWidth: '88%', backgroundColor: 'background.paper' }}>
              <Stack spacing={1.25} sx={{ p: 1.25 }}>
                <Box>
                  <Typography variant="body2" sx={{ fontWeight: 800 }}>{t('editor.pluginDev.currentProposal')}</Typography>
                  <Typography variant="caption" color="text.secondary">{task?.summary || t('editor.pluginDev.reviewHint')}</Typography>
                </Box>
                <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }}>
                  <ActionButton tone="ghost" onClick={onClearProposal} disabled={isApplyingProposal || (!generatedCode && !task)}>{t('editor.pluginDev.clearProposal')}</ActionButton>
                  <ActionButton onClick={onApplyToEditor} disabled={!proposalReady || isApplyingProposal}>{t('editor.pluginDev.applyToEditor')}</ActionButton>
                  <ActionButton tone="primary" startIcon={<SaveIcon />} onClick={onApplyProposal} disabled={!canApplyProposal || isApplyingProposal}>{isApplyingProposal ? t('editor.applying') : t('editor.pluginDev.applyProposal')}</ActionButton>
                </Stack>
              </Stack>
              <Box sx={{ height: { xs: 280, md: 340 }, borderTop: 1, borderColor: 'divider' }}>
                {task?.diff ? (
                  <DiffViewer diff={task.diff} />
                ) : (
                  <Editor
                    height="100%"
                    defaultLanguage="python"
                    theme={theme.palette.mode === 'dark' ? 'vs-dark' : 'light'}
                    value={generatedCode || t('editor.pluginDev.placeholder')}
                    options={{ readOnly: true, minimap: { enabled: false }, fontSize: 13, wordWrap: 'on', automaticLayout: true, scrollBeyondLastLine: false }}
                  />
                )}
              </Box>
            </Paper>
          </Stack>
        )}

        {isGenerating && (
          <Box sx={{ width: '100%', display: 'flex', justifyContent: 'center', py: 1 }}>
            <CircularProgress size={18} />
          </Box>
        )}
      </Stack>

      <Box sx={{ p: 1.5, borderTop: 1, borderColor: 'divider', backgroundColor: alpha(theme.palette.background.paper, 0.88) }}>
        <Stack direction="row" spacing={1} alignItems="flex-end">
          <TextField
            value={prompt}
            onChange={event => onPromptChange(event.target.value)}
            onKeyDown={handleKeyDown}
            onCompositionStart={() => { composerRef.current = true }}
            onCompositionEnd={() => { composerRef.current = false }}
            placeholder={t('editor.pluginDev.chatPlaceholder')}
            multiline
            maxRows={5}
            size="small"
            fullWidth
            disabled={isApplyingProposal}
          />
          <IconActionButton tone="primary" onClick={onSend} disabled={!canSend} sx={{ width: 40, height: 40 }}>
            {isGenerating ? <CircularProgress size={18} color="inherit" /> : <SendIcon fontSize="small" />}
          </IconActionButton>
        </Stack>
      </Box>
    </Paper>
  )
}

interface HistoryDialogProps {
  open: boolean
  filePath: string
  history: PluginDevHistoryResponse | null
  isLoading: boolean
  isRollingBack: boolean
  onClose: () => void
  onRollback: (versionId: string, target: RollbackTarget) => void
  t: Translate
}

function HistoryDialog({
  open,
  filePath,
  history,
  isLoading,
  isRollingBack,
  onClose,
  onRollback,
  t,
}: HistoryDialogProps) {
  const versions = [...(history?.versions ?? [])].reverse()

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>{t('editor.pluginDev.historyTitle')}</DialogTitle>
      <DialogContent>
        <Stack spacing={1.5} sx={{ pt: 1 }}>
          <DialogContentText>
            {filePath || t('editor.pluginDev.noFileSelected')}
          </DialogContentText>
          {isLoading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
              <CircularProgress size={24} />
            </Box>
          ) : versions.length ? (
            versions.map(version => (
              <Paper
                key={version.version_id}
                variant="outlined"
                sx={{ p: 1.5, borderRadius: BORDER_RADIUS.DEFAULT }}
              >
                <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5} alignItems={{ xs: 'stretch', sm: 'center' }}>
                  <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                    <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5, flexWrap: 'wrap' }}>
                      <Typography variant="subtitle2" sx={{ fontWeight: 800 }}>
                        {version.version_id}
                      </Typography>
                      {history?.current_version_id === version.version_id && (
                        <StatusChip label={t('editor.pluginDev.currentVersion')} tone="success" />
                      )}
                      <StatusChip label={version.plugin_api_version || t('editor.pluginDev.unknownApi')} tone="info" />
                    </Stack>
                    <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                      {version.summary || t('editor.pluginDev.noSummary')}
                    </Typography>
                    <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
                      {version.created_at}
                    </Typography>
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      display="block"
                      sx={{ fontFamily: 'Consolas, Monaco, monospace' }}
                    >
                      {version.before_sha256.slice(0, 12)} {'->'} {version.after_sha256.slice(0, 12)}
                    </Typography>
                  </Box>
                  <Stack direction="row" spacing={1} sx={{ justifyContent: { xs: 'flex-start', sm: 'flex-end' }, flexWrap: 'wrap' }}>
                    <ActionButton
                      size="small"
                      startIcon={<RestoreIcon />}
                      onClick={() => onRollback(version.version_id, 'before')}
                      disabled={isRollingBack}
                    >
                      {t('editor.pluginDev.rollbackBefore')}
                    </ActionButton>
                    <ActionButton
                      size="small"
                      startIcon={<RestoreIcon />}
                      onClick={() => onRollback(version.version_id, 'after')}
                      disabled={isRollingBack}
                    >
                      {t('editor.pluginDev.rollbackAfter')}
                    </ActionButton>
                  </Stack>
                </Stack>
              </Paper>
            ))
          ) : (
            <Alert severity="info">{t('editor.pluginDev.noHistory')}</Alert>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <ActionButton onClick={onClose}>{t('editor.cancel')}</ActionButton>
      </DialogActions>
    </Dialog>
  )
}

export default function PluginCcEditorPage() {
  const notification = useNotification()
  const { t } = useTranslation('plugins')

  const [files, setFiles] = useState<string[]>([])
  const [selectedFile, setSelectedFile] = useState('')
  const [code, setCode] = useState('')
  const [originalCode, setOriginalCode] = useState('')
  const [prompt, setPrompt] = useState('')
  const [generatedCode, setGeneratedCode] = useState('')

  const [status, setStatus] = useState<PluginDevStatusResponse | null>(null)
  const [task, setTask] = useState<PluginDevTaskResponse | null>(null)
  const [activeTaskId, setActiveTaskId] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [isApplyingProposal, setIsApplyingProposal] = useState(false)
  const [isTaskStreamFallback, setIsTaskStreamFallback] = useState(false)
  const [configOpen, setConfigOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [history, setHistory] = useState<PluginDevHistoryResponse | null>(null)
  const [isHistoryLoading, setIsHistoryLoading] = useState(false)
  const [isRollingBack, setIsRollingBack] = useState(false)
  const [newPluginOpen, setNewPluginOpen] = useState(false)
  const [newPluginName, setNewPluginName] = useState('')
  const [newPluginDescription, setNewPluginDescription] = useState('')
  const [newPluginCreateMode, setNewPluginCreateMode] = useState<NewPluginCreateMode>('file')
  const [ccPresets, setCcPresets] = useState<CCModelPresetInfo[]>([])
  const [selectedPreset, setSelectedPreset] = useState<CCModelPresetInfo | null>(null)
  const restoredTaskIdRef = useRef<string | null>(null)
  const pollingTaskIdRef = useRef<string | null>(null)
  const pollingRunIdRef = useRef(0)
  const taskStreamCleanupRef = useRef<(() => void) | null>(null)
  const taskStreamFallbackRef = useRef(false)
  const terminalNotifiedTaskIdsRef = useRef<Set<string>>(new Set())
  const mountedRef = useRef(false)

  const hasPendingProposal = task?.status === 'waiting_apply'
  const hasRunningTask = Boolean(task && RUNNING_TASK_STATUSES.has(task.status))
  const currentTaskId = task?.task_id || activeTaskId
  const proposalReady = Boolean(task?.proposal_id && task.result_code)
  const canApplyProposal = proposalReady && hasPendingProposal
  const hasLocalChanges = code !== originalCode
  const isBusy = isGenerating || isApplyingProposal
  const isInteractionLocked = isBusy || hasPendingProposal || hasRunningTask
  const canStopTask = Boolean(currentTaskId && (isGenerating || hasRunningTask))
  const canReconnectTaskStream = Boolean(currentTaskId && (isGenerating || hasRunningTask || isTaskStreamFallback))

  const loadFiles = async () => {
    try {
      const pluginFiles = await pluginEditorApi.getPluginFiles()
      setFiles(pluginFiles)
      const draft = safeParseDraft(window.localStorage.getItem(PLUGIN_CC_EDITOR_DRAFT_KEY))
      const draftFile = draft?.selectedFile && pluginFiles.includes(draft.selectedFile) ? draft.selectedFile : ''
      if (draftFile) {
        setSelectedFile(draftFile)
        setCode(draft.code || '')
        setOriginalCode(draft.originalCode || '')
        setPrompt(draft.prompt || '')
        setGeneratedCode(draft.generatedCode || '')
        setActiveTaskId(draft.taskId || '')
        restoredTaskIdRef.current = draft.taskId || null
        return
      }
      if (!selectedFile && pluginFiles[0]) {
        setSelectedFile(pluginFiles[0])
        const content = await pluginEditorApi.getPluginFileContent(pluginFiles[0])
        setCode(content || '')
        setOriginalCode(content || '')
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.messages.loadFileListFailed')}: ${message}`)
    }
  }

  const loadStatus = async () => {
    try {
      const [nextStatus, presets] = await Promise.all([pluginDevApi.getStatus(), ccModelPresetApi.getList()])
      setStatus(nextStatus)
      setCcPresets(presets)
      setSelectedPreset(
        presets.find(preset => preset.id === nextStatus.cc_model_preset_id) ??
          presets.find(preset => preset.is_default) ??
          presets[0] ??
          null
      )
    } catch (error) {
      setStatus(null)
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.pluginDev.statusLoadFailed')}: ${message}`)
    }
  }

  useEffect(() => {
    mountedRef.current = true
    loadFiles()
    loadStatus()
    return () => {
      mountedRef.current = false
      taskStreamCleanupRef.current?.()
      taskStreamCleanupRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!selectedFile) return
    const draft: PluginCcEditorDraft = {
      selectedFile,
      code,
      originalCode,
      prompt,
      generatedCode,
      taskId: task?.task_id || activeTaskId,
    }
    window.localStorage.setItem(PLUGIN_CC_EDITOR_DRAFT_KEY, JSON.stringify(draft))
  }, [selectedFile, code, originalCode, prompt, generatedCode, task?.task_id, activeTaskId])

  useEffect(() => {
    const taskId = restoredTaskIdRef.current
    if (!taskId || pollingTaskIdRef.current === taskId) return
    restoredTaskIdRef.current = null
    setActiveTaskId(taskId)
    startTaskStream(taskId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedFile])

  const handleFileSelect = async (event: SelectChangeEvent<string>) => {
    const file = event.target.value
    stopTaskStream()
    stopTaskPolling()
    restoredTaskIdRef.current = null
    setSelectedFile(file)
    try {
      const content = await pluginEditorApi.getPluginFileContent(file)
      setCode(content || '')
      setOriginalCode(content || '')
      setGeneratedCode('')
      setTask(null)
      setActiveTaskId('')
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.messages.loadContentFailed')}: ${message}`)
    }
  }

  const handleCodeChange = (value: string) => {
    setCode(value)
  }

  const handleStartSandbox = async () => {
    try {
      const nextStatus = await pluginDevApi.start()
      setStatus(nextStatus)
      notification.success(t('editor.messages.pluginDevStarted'))
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.messages.pluginDevStartFailed')}: ${message}`)
    }
  }

  const handleSaveConfig = async () => {
    try {
      const nextStatus = await pluginDevApi.setCCModelPreset(selectedPreset?.id ?? null)
      setStatus(nextStatus)
      setConfigOpen(false)
      notification.success(t('editor.messages.pluginDevConfigSaved'))
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.messages.pluginDevConfigSaveFailed')}: ${message}`)
    }
  }

  const stopTaskStream = () => {
    taskStreamCleanupRef.current?.()
    taskStreamCleanupRef.current = null
    taskStreamFallbackRef.current = false
    setIsTaskStreamFallback(false)
  }

  const stopTaskPolling = () => {
    pollingRunIdRef.current += 1
    pollingTaskIdRef.current = null
  }

  const applyTaskSnapshot = (nextTask: PluginDevTaskResponse): boolean => {
    setTask(nextTask)
    if (nextTask.diff || nextTask.result_code) {
      setGeneratedCode(nextTask.diff || nextTask.result_code)
    } else {
      setGeneratedCode([`${t('editor.pluginDev.progress')}: ${nextTask.status}`, ...nextTask.logs].join('\n'))
    }

    const completed = !RUNNING_TASK_STATUSES.has(nextTask.status)
    if (completed) {
      if (!terminalNotifiedTaskIdsRef.current.has(nextTask.task_id)) {
        terminalNotifiedTaskIdsRef.current.add(nextTask.task_id)
        if (nextTask.status === 'waiting_apply') notification.success(t('editor.messages.pluginDevProposalReady'))
        if (nextTask.status === 'failed') notification.error(nextTask.error || t('editor.messages.pluginDevGenerateFailed'))
      }
      setIsGenerating(false)
    }
    return completed
  }

  const startTaskStream = (taskId: string) => {
    stopTaskStream()
    pollingTaskIdRef.current = taskId
    taskStreamFallbackRef.current = false
    setIsTaskStreamFallback(false)
    setIsGenerating(true)

    try {
      taskStreamCleanupRef.current = streamPluginDevTask(
        taskId,
        event => {
          if (!mountedRef.current || pollingTaskIdRef.current !== taskId) return
          if (event.type === 'task') {
            const completed = applyTaskSnapshot(event.task)
            if (completed) {
              stopTaskStream()
              stopTaskPolling()
              setIsTaskStreamFallback(false)
            }
            return
          }
          if (event.type === 'done') {
            stopTaskStream()
            stopTaskPolling()
            setIsTaskStreamFallback(false)
            setIsGenerating(false)
          }
        },
        error => {
          if (!mountedRef.current || pollingTaskIdRef.current !== taskId || taskStreamFallbackRef.current) return
          taskStreamFallbackRef.current = true
          setIsTaskStreamFallback(true)
          taskStreamCleanupRef.current?.()
          taskStreamCleanupRef.current = null
          notification.warning(`${t('editor.pluginDev.streamFallback')}: ${error.message}`)
          void pollTask(taskId)
        }
      )
    } catch (error) {
      taskStreamFallbackRef.current = true
      setIsTaskStreamFallback(true)
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.warning(`${t('editor.pluginDev.streamFallback')}: ${message}`)
      void pollTask(taskId)
    }
  }

  const loadHistoryForFile = async (filePath: string) => {
    if (!filePath) {
      setHistory(null)
      return
    }
    setIsHistoryLoading(true)
    try {
      const nextHistory = await pluginDevApi.getHistory(filePath)
      setHistory(nextHistory)
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.pluginDev.historyLoadFailed')}: ${message}`)
      setHistory(null)
    } finally {
      setIsHistoryLoading(false)
    }
  }

  const handleOpenHistory = () => {
    const historyFile = task?.file_path || selectedFile
    if (!historyFile) {
      notification.error(t('editor.messages.selectPluginFirst'))
      return
    }
    setHistoryOpen(true)
    void loadHistoryForFile(historyFile)
  }

  const handleRollbackVersion = async (versionId: string, target: RollbackTarget) => {
    const historyFile = history?.file_path || task?.file_path || selectedFile
    if (!historyFile) return

    setIsRollingBack(true)
    try {
      const response = await pluginDevApi.rollback(historyFile, versionId, target)
      const [content, pluginFiles] = await Promise.all([
        pluginEditorApi.getPluginFileContent(historyFile),
        pluginEditorApi.getPluginFiles(),
      ])
      stopTaskStream()
      stopTaskPolling()
      restoredTaskIdRef.current = null
      setFiles(pluginFiles)
      setSelectedFile(historyFile)
      setCode(content || '')
      setOriginalCode(content || '')
      setTask(null)
      setActiveTaskId('')
      setGeneratedCode('')
      await loadHistoryForFile(historyFile)
      notification.success(t('editor.messages.pluginDevRollbackSuccess', { version: response.version_id }))
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.messages.applyFailed')}: ${message}`)
    } finally {
      setIsRollingBack(false)
    }
  }

  const pollTask = async (taskId: string) => {
    const runId = pollingRunIdRef.current + 1
    pollingRunIdRef.current = runId
    pollingTaskIdRef.current = taskId
    try {
      while (mountedRef.current && pollingTaskIdRef.current === taskId && pollingRunIdRef.current === runId) {
        const nextTask = await pluginDevApi.getTask(taskId)
        if (!mountedRef.current || pollingTaskIdRef.current !== taskId || pollingRunIdRef.current !== runId) return
        if (applyTaskSnapshot(nextTask)) {
          setIsTaskStreamFallback(false)
          return
        }
        await new Promise(resolve => window.setTimeout(resolve, 1500))
      }
    } catch (error) {
      if (!mountedRef.current || pollingTaskIdRef.current !== taskId || pollingRunIdRef.current !== runId) return
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.messages.pluginDevGenerateFailed')}: ${message}`)
      setIsGenerating(false)
    } finally {
      if (pollingTaskIdRef.current === taskId && pollingRunIdRef.current === runId) {
        pollingTaskIdRef.current = null
      }
    }
  }

  const handleCreatePlugin = async () => {
    const name = newPluginName.trim()
    const description = newPluginDescription.trim()
    if (!name.match(/^[a-z][a-z0-9_]*$/)) {
      notification.error(t('editor.validation.invalidName'))
      return
    }
    if (!description) {
      notification.error(t('editor.validation.emptyDescription'))
      return
    }
    try {
      const template = await pluginEditorApi.generatePluginTemplate(name, description)
      const selectedPluginFile = newPluginCreateMode === 'folder' ? `${name}/plugin.py` : `${name}.py`
      if (newPluginCreateMode === 'folder') {
        await pluginEditorApi.savePluginFile(`${name}/__init__.py`, 'from .plugin import plugin\n')
        await pluginEditorApi.savePluginFile(selectedPluginFile, template || '')
      } else {
        await pluginEditorApi.savePluginFile(selectedPluginFile, template || '')
      }
      const pluginFiles = await pluginEditorApi.getPluginFiles()
      stopTaskStream()
      stopTaskPolling()
      restoredTaskIdRef.current = null
      setFiles(pluginFiles)
      setSelectedFile(selectedPluginFile)
      setCode(template || '')
      setOriginalCode(template || '')
      setPrompt('')
      setGeneratedCode('')
      setTask(null)
      setActiveTaskId('')
      setNewPluginOpen(false)
      setNewPluginName('')
      setNewPluginDescription('')
      setNewPluginCreateMode('file')
      notification.success(t('editor.messages.createSuccess'))
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.messages.createFailed')}: ${message}`)
    }
  }

  const handleGenerate = async () => {
    if (!selectedFile) {
      notification.error(t('editor.messages.selectPluginFirst'))
      return
    }
    if (hasPendingProposal) {
      notification.warning(t('editor.pluginDev.waitingApply'))
      return
    }
    if (!prompt.trim()) {
      notification.error(t('editor.messages.enterPrompt'))
      return
    }
    const submittedPrompt = prompt.trim()
    setIsGenerating(true)
    setTask(null)
    setActiveTaskId('')
    setGeneratedCode(t('editor.pluginDev.waiting'))
    setPrompt('')
    try {
      const response = await pluginDevApi.generate({
        file_path: selectedFile,
        prompt: submittedPrompt,
        current_code: code,
        base_code: originalCode,
        dirty: hasLocalChanges,
        mode: 'proposal',
      })
      restoredTaskIdRef.current = response.task_id
      setActiveTaskId(response.task_id)
      startTaskStream(response.task_id)
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      setIsGenerating(false)
      notification.error(`${t('editor.messages.pluginDevGenerateFailed')}: ${message}`)
    }
  }

  const handleStopTask = async () => {
    if (!currentTaskId) return
    const taskId = currentTaskId
    stopTaskStream()
    stopTaskPolling()
    setIsGenerating(false)
    try {
      const cancelledTask = await pluginDevApi.cancelTask(taskId)
      applyTaskSnapshot(cancelledTask)
      setActiveTaskId('')
      notification.info(t('editor.messages.pluginDevTaskStopped'))
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.messages.pluginDevTaskStopFailed')}: ${message}`)
    }
  }

  const handleReconnectTaskStream = () => {
    if (!currentTaskId) return
    stopTaskPolling()
    startTaskStream(currentTaskId)
    notification.info(t('editor.messages.pluginDevStreamReconnected'))
  }

  const handleApplyToEditor = async () => {
    if (!task?.result_code) return

    const proposalFile = task.file_path
    const fileContent = proposalFile === selectedFile
      ? originalCode
      : await pluginEditorApi.getPluginFileContent(proposalFile)

    setSelectedFile(proposalFile)
    setOriginalCode(fileContent || '')
    setCode(task.result_code)
    notification.success(t('editor.messages.applySuccess'))
  }

  const handleClearProposal = async () => {
    const currentTask = task
    const taskId = currentTask?.task_id || activeTaskId

    stopTaskPolling()
    restoredTaskIdRef.current = null
    stopTaskStream()

    try {
      if (taskId && (isGenerating || (currentTask && RUNNING_TASK_STATUSES.has(currentTask.status)))) {
        await pluginDevApi.cancelTask(taskId)
      } else if (currentTask?.proposal_id && currentTask.status === 'waiting_apply') {
        await pluginDevApi.discardProposal(currentTask.proposal_id)
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(message)
    }

    setTask(null)
    setActiveTaskId('')
    setGeneratedCode('')
    setIsGenerating(false)
    notification.info(t('editor.messages.generateCleared'))
  }

  const handleApplyProposal = async () => {
    if (!task?.proposal_id) return

    const proposalId = task.proposal_id
    const proposalFile = task.file_path

    setIsApplyingProposal(true)
    try {
      const response = await pluginDevApi.applyProposal(proposalId)
      const content = await pluginEditorApi.getPluginFileContent(proposalFile)
      setSelectedFile(proposalFile)
      setCode(content || '')
      setOriginalCode(content || '')
      setTask(prev => prev ? { ...prev, status: 'applied' } : prev)
      setActiveTaskId('')
      setGeneratedCode(content || '')
      if (historyOpen) {
        await loadHistoryForFile(proposalFile)
      }
      notification.success(t('editor.messages.pluginDevProposalApplied', { version: response.version_id }))
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.messages.applyFailed')}: ${message}`)
    } finally {
      setIsApplyingProposal(false)
    }
  }

  return (
    <Box sx={{ height: 'calc(100vh - 64px)', minHeight: 0, overflow: 'auto', p: 2, boxSizing: 'border-box', display: 'flex', flexDirection: 'column', gap: 2 }}>
      <TaskStatusHeader
        status={status}
        task={task}
        selectedFile={task?.file_path || selectedFile}
        isGenerating={isGenerating}
        isApplyingProposal={isApplyingProposal}
        canGenerate={!isGenerating && !hasPendingProposal && Boolean(prompt.trim() && selectedFile)}
        canOpenHistory={Boolean(task?.file_path || selectedFile)}
        canStopTask={canStopTask}
        canReconnectTaskStream={canReconnectTaskStream}
        onOpenConfig={() => setConfigOpen(true)}
        onOpenHistory={handleOpenHistory}
        onStartSandbox={handleStartSandbox}
        onStopTask={handleStopTask}
        onReconnectTaskStream={handleReconnectTaskStream}
        onGenerate={handleGenerate}
        t={t}
      />

      <Grid container spacing={2} sx={{ flex: { xs: '0 0 auto', xl: 1 }, minHeight: { xs: 'auto', xl: 0 }, alignItems: 'stretch' }}>
        <Grid size={{ xs: 12, xl: 5 }} sx={{ minHeight: { xs: 520, xl: 0 }, display: 'flex' }}>
          <EditorContextPanel
            files={files}
            selectedFile={selectedFile}
            code={code}
            hasLocalChanges={hasLocalChanges}
            isBusy={isInteractionLocked}
            onFileSelect={handleFileSelect}
            onOpenCreatePlugin={() => setNewPluginOpen(true)}
            onCodeChange={handleCodeChange}
            t={t}
          />
        </Grid>
        <Grid size={{ xs: 12, xl: 7 }} sx={{ minHeight: { xs: 560, xl: 0 }, display: 'flex' }}>
          <CcConversationPanel
            prompt={prompt}
            generatedCode={generatedCode}
            task={task}
            status={status}
            isGenerating={isGenerating}
            isApplyingProposal={isApplyingProposal}
            proposalReady={proposalReady}
            canApplyProposal={canApplyProposal}
            onPromptChange={setPrompt}
            onSend={handleGenerate}
            onClearProposal={handleClearProposal}
            onApplyToEditor={handleApplyToEditor}
            onApplyProposal={handleApplyProposal}
            t={t}
          />
        </Grid>
      </Grid>

      <Dialog open={newPluginOpen} onClose={() => setNewPluginOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t('editor.newPlugin')}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ pt: 1 }}>
            <TextField
              label={t('editor.pluginName')}
              value={newPluginName}
              onChange={event => setNewPluginName(event.target.value)}
              helperText={t('editor.pluginNameHelper')}
              fullWidth
              required
            />
            <TextField
              label={t('editor.pluginDescription')}
              value={newPluginDescription}
              onChange={event => setNewPluginDescription(event.target.value)}
              fullWidth
              required
              multiline
              rows={2}
            />
            <FormControl fullWidth size="small">
              <InputLabel>{t('editor.createMode')}</InputLabel>
              <Select
                value={newPluginCreateMode}
                label={t('editor.createMode')}
                onChange={event => setNewPluginCreateMode(event.target.value as NewPluginCreateMode)}
              >
                <MenuItem value="file">{t('editor.createModeFile')}</MenuItem>
                <MenuItem value="folder">{t('editor.createModeFolder')}</MenuItem>
              </Select>
            </FormControl>
          </Stack>
        </DialogContent>
        <DialogActions>
          <ActionButton onClick={() => setNewPluginOpen(false)}>{t('editor.cancel')}</ActionButton>
          <ActionButton tone="primary" onClick={handleCreatePlugin}>{t('editor.create')}</ActionButton>
        </DialogActions>
      </Dialog>

      <HistoryDialog
        open={historyOpen}
        filePath={history?.file_path || task?.file_path || selectedFile}
        history={history}
        isLoading={isHistoryLoading}
        isRollingBack={isRollingBack}
        onClose={() => setHistoryOpen(false)}
        onRollback={handleRollbackVersion}
        t={t}
      />

      <Dialog open={configOpen} onClose={() => setConfigOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle>{t('editor.pluginDev.configTitle')}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ pt: 1 }}>
            <DialogContentText>{t('editor.pluginDev.configDescription')}</DialogContentText>
            <Autocomplete
              options={ccPresets}
              value={selectedPreset}
              disableClearable
              onChange={(_, value) => setSelectedPreset(value)}
              getOptionLabel={option => option.name}
              isOptionEqualToValue={(option, value) => option.id === value.id}
              renderInput={params => <TextField {...params} label={t('editor.pluginDev.modelPreset')} size="small" />}
            />
            {selectedPreset && <TextField fullWidth multiline rows={8} size="small" value={JSON.stringify(selectedPreset.config_json, null, 2)} InputProps={{ readOnly: true }} label={t('editor.pluginDev.generatedConfig')} />}
          </Stack>
        </DialogContent>
        <DialogActions>
          <ActionButton onClick={() => setConfigOpen(false)}>{t('editor.cancel')}</ActionButton>
          <ActionButton tone="primary" onClick={handleSaveConfig} disabled={!selectedPreset}>{t('actions.save')}</ActionButton>
        </DialogActions>
      </Dialog>
    </Box>
  )
}
