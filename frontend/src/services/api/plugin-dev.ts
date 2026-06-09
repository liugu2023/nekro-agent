import axios from './axios'
import { createEventStream } from './utils/stream'

const encodePathParam = (filePath: string): string => filePath.split('/').map(encodeURIComponent).join('/')

export type PluginDevTaskStatus =
  | 'pending'
  | 'running_cc'
  | 'creating_proposal'
  | 'waiting_apply'
  | 'applied'
  | 'failed'
  | 'cancelled'

export interface PluginDevVersionInfo {
  schema_version: number
  nekro_agent_channel: 'stable' | 'preview'
  nekro_agent_release: string
  nekro_agent_git_commit: string
  source_origin: 'runtime_snapshot' | 'cached_runtime' | 'remote_git' | 'cached_remote' | 'disabled' | 'unavailable'
  source_repo_url: string
  source_ref: string
  source_resolved_commit: string
  source_path: string
  source_dirty: boolean
  source_locked_at: string
  plugin_api_version: string
  stable_plugin_api_version: string
  template_version: string
  updated_at: string
  notes: string
}

export interface PluginDevStatusResponse {
  enabled: boolean
  sandbox_status: 'not_implemented' | 'stopped' | 'running' | 'failed'
  active_task_id: string | null
  queue_length: number
  cc_model_preset_id: number | null
  cc_model_preset_name: string | null
  version: PluginDevVersionInfo
}

export interface PluginDevGenerateRequest {
  file_path: string
  prompt: string
  current_code: string
  base_code: string
  dirty: boolean
  mode?: 'proposal'
}

export interface PluginDevGenerateResponse {
  task_id: string
  status: PluginDevTaskStatus
  proposal_id: string | null
}

export interface PluginDevTaskResponse {
  task_id: string
  file_path: string
  status: PluginDevTaskStatus
  summary: string
  logs: string[]
  proposal_id: string | null
  diff: string
  result_code: string
  error: string
  version: PluginDevVersionInfo
}

export interface PluginDevProposalResponse {
  proposal_id: string
  task_id: string
  file_path: string
  status: 'pending' | 'applied' | 'discarded'
  diff: string
  result_code: string
  summary: string
  created_at: string
}

export interface PluginDevApplyResponse {
  ok: boolean
  version_id: string
}

export interface PluginDevHistoryItem {
  version_id: string
  task_id: string
  action: string
  before_sha256: string
  after_sha256: string
  plugin_api_version: string
  nekro_agent_git_commit: string
  created_at: string
  summary: string
}

export interface PluginDevHistoryResponse {
  file_path: string
  current_version_id: string | null
  versions: PluginDevHistoryItem[]
}

export type PluginDevTaskStreamEvent =
  | { type: 'task'; task: PluginDevTaskResponse }
  | { type: 'done'; status: PluginDevTaskStatus }

export const pluginDevApi = {
  getStatus: async (): Promise<PluginDevStatusResponse> => {
    const response = await axios.get<PluginDevStatusResponse>('/plugin-dev/status')
    return response.data
  },

  start: async (): Promise<PluginDevStatusResponse> => {
    const response = await axios.post<PluginDevStatusResponse>('/plugin-dev/start')
    return response.data
  },

  stop: async (): Promise<PluginDevStatusResponse> => {
    const response = await axios.post<PluginDevStatusResponse>('/plugin-dev/stop')
    return response.data
  },

  getVersion: async (): Promise<PluginDevVersionInfo> => {
    const response = await axios.get<PluginDevVersionInfo>('/plugin-dev/version')
    return response.data
  },

  setCCModelPreset: async (presetId: number | null): Promise<PluginDevStatusResponse> => {
    const response = await axios.put<PluginDevStatusResponse>('/plugin-dev/cc-model-preset', {
      cc_model_preset_id: presetId,
    })
    return response.data
  },

  generate: async (body: PluginDevGenerateRequest): Promise<PluginDevGenerateResponse> => {
    const response = await axios.post<PluginDevGenerateResponse>('/plugin-dev/generate', body, {
      timeout: 120000,
    })
    return response.data
  },

  getTask: async (taskId: string): Promise<PluginDevTaskResponse> => {
    const response = await axios.get<PluginDevTaskResponse>(`/plugin-dev/tasks/${encodeURIComponent(taskId)}`)
    return response.data
  },

  cancelTask: async (taskId: string): Promise<PluginDevTaskResponse> => {
    const response = await axios.post<PluginDevTaskResponse>(`/plugin-dev/tasks/${encodeURIComponent(taskId)}/cancel`)
    return response.data
  },

  getProposal: async (proposalId: string): Promise<PluginDevProposalResponse> => {
    const response = await axios.get<PluginDevProposalResponse>(`/plugin-dev/proposals/${encodeURIComponent(proposalId)}`)
    return response.data
  },

  applyProposal: async (proposalId: string): Promise<PluginDevApplyResponse> => {
    const response = await axios.post<PluginDevApplyResponse>(`/plugin-dev/proposals/${encodeURIComponent(proposalId)}/apply`)
    return response.data
  },

  discardProposal: async (proposalId: string): Promise<void> => {
    await axios.delete(`/plugin-dev/proposals/${encodeURIComponent(proposalId)}`)
  },

  getHistory: async (filePath: string): Promise<PluginDevHistoryResponse> => {
    const response = await axios.get<PluginDevHistoryResponse>(`/plugin-dev/history/${encodePathParam(filePath)}`)
    return response.data
  },

  rollback: async (filePath: string, versionId: string, target: 'before' | 'after' = 'before'): Promise<PluginDevApplyResponse> => {
    const response = await axios.post<PluginDevApplyResponse>(`/plugin-dev/rollback/${encodePathParam(filePath)}`, {
      version_id: versionId,
      target,
    })
    return response.data
  },
}

export const streamPluginDevTask = (
  taskId: string,
  onEvent: (event: PluginDevTaskStreamEvent) => void,
  onError?: (error: Error) => void,
  signal?: AbortSignal
) => {
  return createEventStream({
    endpoint: `/plugin-dev/tasks/${encodeURIComponent(taskId)}/stream`,
    onMessage: data => {
      const trimmedData = data.trim()
      if (!trimmedData || !trimmedData.startsWith('{')) return
      try {
        const event = JSON.parse(trimmedData) as PluginDevTaskStreamEvent
        onEvent(event)
      } catch (error) {
        console.warn('Failed to parse plugin dev task stream event:', error, trimmedData)
      }
    },
    onError,
    signal,
  })
}
