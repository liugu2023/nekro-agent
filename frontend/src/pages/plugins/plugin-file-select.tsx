import { Block as BlockIcon, FolderOutlined as FolderOutlinedIcon } from '@mui/icons-material'
import { Box, ListSubheader, MenuItem, alpha } from '@mui/material'
import { ReactNode } from 'react'

export interface PluginFileEntry {
  /** 完整相对路径（Select 的 value，如 mypkg/plugin.py 或 demo.py.disabled） */
  path: string
  /** 展示名：包内文件为包内相对路径，单文件为文件名（已去掉 .disabled 后缀） */
  displayName: string
  isDisabled: boolean
  inPackage: boolean
}

export interface PluginFileGroup {
  /** 顶层条目名：包插件为目录名，单文件插件为文件名 */
  topName: string
  isPackage: boolean
  entries: PluginFileEntry[]
}

const stripDisabledSuffix = (name: string): string => name.replace(/\.disabled$/, '')

const groupSortKey = (group: PluginFileGroup): string =>
  stripDisabledSuffix(group.topName).replace(/\.py$/, '').toLowerCase()

/**
 * 把扁平的插件文件列表按顶层条目分组：
 * 包插件（含目录的路径）聚合到目录组下，单文件插件独立成组，统一按插件名排序。
 */
export function groupPluginFiles(files: string[]): PluginFileGroup[] {
  const groups = new Map<string, PluginFileGroup>()
  for (const file of files) {
    const separatorIndex = file.indexOf('/')
    const isPackage = separatorIndex > 0
    const topName = isPackage ? file.slice(0, separatorIndex) : file
    const key = `${isPackage ? 'pkg' : 'file'}:${topName}`
    let group = groups.get(key)
    if (!group) {
      group = { topName, isPackage, entries: [] }
      groups.set(key, group)
    }
    const innerName = isPackage ? file.slice(separatorIndex + 1) : file
    group.entries.push({
      path: file,
      displayName: stripDisabledSuffix(innerName),
      isDisabled: file.endsWith('.disabled'),
      inPackage: isPackage,
    })
  }

  const sortedGroups = [...groups.values()].sort((a, b) => groupSortKey(a).localeCompare(groupSortKey(b)))
  for (const group of sortedGroups) {
    group.entries.sort((a, b) => {
      const aInitRank = a.displayName === '__init__.py' ? 0 : 1
      const bInitRank = b.displayName === '__init__.py' ? 0 : 1
      if (aInitRank !== bInitRank) return aInitRank - bInitRank
      return a.displayName.toLowerCase().localeCompare(b.displayName.toLowerCase())
    })
  }
  return sortedGroups
}

/**
 * 渲染分组后的插件文件下拉项（MUI Select 的 children 需要扁平的元素数组，
 * 因此返回 ReactNode[] 而不是包装组件）。
 */
export function renderPluginFileMenuItems(files: string[], disabledLabel: string): ReactNode[] {
  const nodes: ReactNode[] = []
  for (const group of groupPluginFiles(files)) {
    if (group.isPackage) {
      nodes.push(
        <ListSubheader
          key={`pkg:${group.topName}`}
          disableSticky
          sx={{
            display: 'flex',
            alignItems: 'center',
            gap: 0.75,
            lineHeight: '32px',
            fontWeight: 700,
            color: 'text.secondary',
            bgcolor: 'transparent',
          }}
        >
          <FolderOutlinedIcon fontSize="small" sx={{ opacity: 0.8 }} />
          {group.topName}/
        </ListSubheader>
      )
    }
    for (const entry of group.entries) {
      nodes.push(
        <MenuItem
          key={entry.path}
          value={entry.path}
          sx={{
            color: entry.isDisabled ? 'text.disabled' : 'text.primary',
            ...(entry.isDisabled && {
              background: theme =>
                theme.palette.mode === 'dark' ? 'rgba(255, 0, 0, 0.08)' : 'rgba(255, 0, 0, 0.05)',
              fontStyle: 'italic',
            }),
            '&.Mui-selected': {
              backgroundColor: theme =>
                alpha(theme.palette.primary.main, theme.palette.mode === 'dark' ? 0.25 : 0.1),
              color: theme =>
                theme.palette.mode === 'dark' ? theme.palette.primary.light : theme.palette.primary.main,
              fontWeight: 'bold',
            },
          }}
        >
          <Box
            component="span"
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 1,
              minWidth: 0,
              pl: entry.inPackage ? 2.5 : 0,
            }}
          >
            {entry.isDisabled && <BlockIcon color="error" fontSize="small" sx={{ opacity: 0.7 }} />}
            <Box component="span" sx={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {entry.isDisabled ? `${entry.displayName} (${disabledLabel})` : entry.displayName}
            </Box>
          </Box>
        </MenuItem>
      )
    }
  }
  return nodes
}
