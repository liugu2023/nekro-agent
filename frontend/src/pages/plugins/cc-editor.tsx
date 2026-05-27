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
  PlayArrow as PlayArrowIcon,
  Save as SaveIcon,
  Send as SendIcon,
  Settings as SettingsIcon,
  SmartToy as SmartToyIcon,
  Terminal as TerminalIcon,
} from '@mui/icons-material'
import { alpha, useTheme } from '@mui/material/styles'
import { Editor } from '@monaco-editor/react'
import { useTranslation } from 'react-i18next'

import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import { ccModelPresetApi, CCModelPresetInfo } from '../../services/api/cc-model-preset'
import { pluginDevApi, PluginDevStatusResponse, PluginDevTaskResponse, PluginDevTaskStatus } from '../../services/api/plugin-dev'
import { pluginEditorApi } from '../../services/api/plugin-editor'
import { useNotification } from '../../hooks/useNotification'
import { BORDER_RADIUS, CARD_STYLES, CHIP_VARIANTS } from '../../theme/variants'

const PLUGIN_CC_EDITOR_DRAFT_KEY = 'nekro-plugin-cc-editor-draft'
const RUNNING_TASK_STATUSES = new Set<PluginDevTaskStatus>(['pending', 'running_cc', 'creating_proposal'])

type NewPluginCreateMode = 'file' | 'folder'

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

interface TaskStatusHeaderProps {
  status: PluginDevStatusResponse | null
  task: PluginDevTaskResponse | null
  selectedFile: string
  isGenerating: boolean
  isApplyingProposal: boolean
  canGenerate: boolean
  canApplyProposal: boolean
  onOpenConfig: () => void
  onStartSandbox: () => void
  onGenerate: () => void
  onApplyProposal: () => void
  t: Translate
}

function TaskStatusHeader({
  status,
  task,
  selectedFile,
  isGenerating,
  isApplyingProposal,
  canGenerate,
  canApplyProposal,
  onOpenConfig,
  onStartSandbox,
  onGenerate,
  onApplyProposal,
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
            </Stack>
            <Typography variant="body2" color="text.secondary" noWrap>
              {selectedFile || t('editor.pluginDev.noFileSelected')} · {status?.cc_model_preset_name || t('editor.pluginDev.defaultPreset')}
              {version ? ` · API ${version.plugin_api_version} · ${version.nekro_agent_channel}` : ''}
            </Typography>
          </Box>
          <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', justifyContent: { xs: 'flex-start', md: 'flex-end' } }}>
            <ActionButton startIcon={<SettingsIcon />} onClick={onOpenConfig} disabled={isGenerating || isApplyingProposal}>
              {t('editor.pluginDev.config')}
            </ActionButton>
            <ActionButton
              startIcon={<PlayArrowIcon />}
              onClick={onStartSandbox}
              disabled={sandboxRunning || isGenerating || isApplyingProposal}
            >
              {t('editor.pluginDev.startSandbox')}
            </ActionButton>
            <ActionButton
              tone="primary"
              startIcon={isGenerating ? <CircularProgress size={16} color="inherit" /> : <AutoAwesomeIcon />}
              onClick={onGenerate}
              disabled={!canGenerate || isApplyingProposal}
            >
              {isGenerating ? t('editor.pluginDev.generating') : t('editor.pluginDev.generate')}
            </ActionButton>
            <ActionButton
              tone="primary"
              startIcon={isApplyingProposal ? <CircularProgress size={16} color="inherit" /> : <SaveIcon />}
              onClick={onApplyProposal}
              disabled={!canApplyProposal || isGenerating}
            >
              {isApplyingProposal ? t('editor.applying') : t('editor.pluginDev.applyProposal')}
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
    <Paper sx={{ ...CARD_STYLES.DEFAULT, p: 2, height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0, gap: 1.5 }}>
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
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
        <CodeIcon fontSize="small" color="primary" />
        <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>{t('editor.pluginDev.sourceSnapshot')}</Typography>
      </Box>
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
  const canSend = Boolean(prompt.trim()) && !isGenerating && !isApplyingProposal && !proposalReady

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing || composerRef.current) return
    event.preventDefault()
    if (canSend) onSend()
  }

  return (
    <Paper sx={{ ...CARD_STYLES.DEFAULT, p: 0, height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <Stack spacing={1.5} sx={{ flex: 1, minHeight: 0, overflow: 'auto', p: 2 }}>
        <Stack direction="row" spacing={1.25} alignItems="flex-start">
          <Box sx={{ width: 32, height: 32, borderRadius: BORDER_RADIUS.DEFAULT, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'primary.main', backgroundColor: alpha(theme.palette.primary.main, 0.12), flexShrink: 0 }}>
            <SmartToyIcon fontSize="small" />
          </Box>
          <Paper variant="outlined" sx={{ p: 1.25, maxWidth: '88%', backgroundColor: 'background.paper' }}>
            <Typography variant="body2">{t('editor.pluginDev.conversationIntro')}</Typography>
            <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
              {status?.version ? `${t('editor.pluginDev.versionFields.release')}: ${status.version.nekro_agent_release || status.version.source_ref || status.version.nekro_agent_channel}` : t('editor.pluginDev.noVersionNotes')}
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

        {(isGenerating || task?.status === 'failed' || task?.status === 'applied') && (
          <Stack direction="row" spacing={1.25} alignItems="flex-start">
            <Box sx={{ width: 32, height: 32, borderRadius: BORDER_RADIUS.DEFAULT, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'primary.main', backgroundColor: alpha(theme.palette.primary.main, 0.12), flexShrink: 0 }}>
              {isGenerating ? <CircularProgress size={16} /> : <SmartToyIcon fontSize="small" />}
            </Box>
            <Paper variant="outlined" sx={{ p: 1.25, maxWidth: '88%', backgroundColor: 'background.paper' }}>
              <Typography variant="body2" sx={{ fontWeight: 700 }}>{t('editor.pluginDev.assistantName')}</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                {task?.status === 'failed'
                  ? task.error || t('editor.messages.pluginDevGenerateFailed')
                  : task?.status === 'applied'
                    ? t('editor.pluginDev.appliedState')
                    : t('editor.pluginDev.generating')}
              </Typography>
            </Paper>
          </Stack>
        )}

        {(task?.diff || generatedCode) && (
          <Paper variant="outlined" sx={{ overflow: 'hidden', flexShrink: 0 }}>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ xs: 'stretch', sm: 'center' }} sx={{ p: 1.25, borderBottom: 1, borderColor: 'divider' }}>
              <Box sx={{ flexGrow: 1 }}>
                <Typography variant="subtitle2" sx={{ fontWeight: 800 }}>{t('editor.pluginDev.currentProposal')}</Typography>
                <Typography variant="caption" color="text.secondary">{task?.summary || t('editor.pluginDev.reviewHint')}</Typography>
              </Box>
              <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }}>
                <ActionButton tone="ghost" onClick={onClearProposal} disabled={isApplyingProposal || (!generatedCode && !task)}>{t('editor.pluginDev.clearProposal')}</ActionButton>
                <ActionButton onClick={onApplyToEditor} disabled={!proposalReady || isApplyingProposal}>{t('editor.pluginDev.applyToEditor')}</ActionButton>
                <ActionButton tone="primary" startIcon={<SaveIcon />} onClick={onApplyProposal} disabled={!canApplyProposal || isApplyingProposal}>{isApplyingProposal ? t('editor.applying') : t('editor.pluginDev.applyProposal')}</ActionButton>
              </Stack>
            </Stack>
            <Box sx={{ height: 360 }}>
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
  const [configOpen, setConfigOpen] = useState(false)
  const [newPluginOpen, setNewPluginOpen] = useState(false)
  const [newPluginName, setNewPluginName] = useState('')
  const [newPluginDescription, setNewPluginDescription] = useState('')
  const [newPluginCreateMode, setNewPluginCreateMode] = useState<NewPluginCreateMode>('file')
  const [ccPresets, setCcPresets] = useState<CCModelPresetInfo[]>([])
  const [selectedPreset, setSelectedPreset] = useState<CCModelPresetInfo | null>(null)
  const restoredTaskIdRef = useRef<string | null>(null)
  const pollingTaskIdRef = useRef<string | null>(null)
  const mountedRef = useRef(false)

  const hasPendingProposal = task?.status === 'waiting_apply'
  const hasRunningTask = Boolean(task && RUNNING_TASK_STATUSES.has(task.status))
  const proposalReady = Boolean(task?.proposal_id && task.result_code)
  const canApplyProposal = proposalReady && hasPendingProposal
  const hasLocalChanges = code !== originalCode
  const isBusy = isGenerating || isApplyingProposal
  const isInteractionLocked = isBusy || hasPendingProposal || hasRunningTask

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
    setIsGenerating(true)
    pollTask(taskId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedFile])

  const handleFileSelect = async (event: SelectChangeEvent<string>) => {
    const file = event.target.value
    pollingTaskIdRef.current = null
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

  const pollTask = async (taskId: string) => {
    pollingTaskIdRef.current = taskId
    try {
      while (mountedRef.current && pollingTaskIdRef.current === taskId) {
        const nextTask = await pluginDevApi.getTask(taskId)
        if (!mountedRef.current || pollingTaskIdRef.current !== taskId) return
        setTask(nextTask)
        if (nextTask.diff || nextTask.result_code) {
          setGeneratedCode(nextTask.diff || nextTask.result_code)
        } else {
          setGeneratedCode([`${t('editor.pluginDev.progress')}: ${nextTask.status}`, ...nextTask.logs].join('\n'))
        }
        if (!RUNNING_TASK_STATUSES.has(nextTask.status)) {
          if (nextTask.status === 'waiting_apply') notification.success(t('editor.messages.pluginDevProposalReady'))
          if (nextTask.status === 'failed') notification.error(nextTask.error || t('editor.messages.pluginDevGenerateFailed'))
          setIsGenerating(false)
          return
        }
        await new Promise(resolve => window.setTimeout(resolve, 1500))
      }
    } catch (error) {
      if (!mountedRef.current || pollingTaskIdRef.current !== taskId) return
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.messages.pluginDevGenerateFailed')}: ${message}`)
      setIsGenerating(false)
    } finally {
      if (pollingTaskIdRef.current === taskId) {
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
      pollingTaskIdRef.current = null
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
      pollTask(response.task_id)
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      setIsGenerating(false)
      notification.error(`${t('editor.messages.pluginDevGenerateFailed')}: ${message}`)
    }
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

    pollingTaskIdRef.current = null
    restoredTaskIdRef.current = null

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
      notification.success(t('editor.messages.pluginDevProposalApplied', { version: response.version_id }))
    } catch (error) {
      const message = error instanceof Error ? error.message : t('editor.messages.unknownError')
      notification.error(`${t('editor.messages.applyFailed')}: ${message}`)
    } finally {
      setIsApplyingProposal(false)
    }
  }

  return (
    <Box sx={{ minHeight: 'calc(100vh - 64px)', p: 2, display: 'flex', flexDirection: 'column', gap: 2 }}>
      <TaskStatusHeader
        status={status}
        task={task}
        selectedFile={task?.file_path || selectedFile}
        isGenerating={isGenerating}
        isApplyingProposal={isApplyingProposal}
        canGenerate={!isGenerating && !hasPendingProposal && Boolean(prompt.trim() && selectedFile)}
        canApplyProposal={canApplyProposal}
        onOpenConfig={() => setConfigOpen(true)}
        onStartSandbox={handleStartSandbox}
        onGenerate={handleGenerate}
        onApplyProposal={handleApplyProposal}
        t={t}
      />

      <Grid container spacing={2} sx={{ flex: 1, minHeight: 0 }}>
        <Grid size={{ xs: 12, xl: 5 }} sx={{ minHeight: 0 }}>
          <EditorContextPanel
            files={files}
            selectedFile={selectedFile}
            code={code}
            hasLocalChanges={hasLocalChanges}
            isBusy={isInteractionLocked}
            onFileSelect={handleFileSelect}
            onOpenCreatePlugin={() => setNewPluginOpen(true)}
            onCodeChange={setCode}
            t={t}
          />
        </Grid>
        <Grid size={{ xs: 12, xl: 7 }} sx={{ minHeight: 0 }}>
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
