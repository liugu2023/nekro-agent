import { useMemo, useRef, useState } from 'react'
import {
  Block as BlockIcon,
  ExpandMore as ExpandMoreIcon,
  ChevronRight as ChevronRightIcon,
  FolderOutlined as FolderOutlinedIcon,
} from '@mui/icons-material'
import {
  Box,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  SelectChangeEvent,
  SxProps,
  Theme,
  alpha,
} from '@mui/material'

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
function groupPluginFiles(files: string[]): PluginFileGroup[] {
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

/** 包展开/收起动作的哨兵 value 前缀（真实文件路径不可能以它开头） */
const PACKAGE_TOGGLE_PREFIX = '__toggle_pkg__:'

const topPackageOf = (filePath: string): string | null => {
  const separatorIndex = filePath.indexOf('/')
  return separatorIndex > 0 ? filePath.slice(0, separatorIndex) : null
}

export interface PluginFileSelectProps {
  files: string[]
  value: string
  onChange: (event: SelectChangeEvent<string>) => void
  label: string
  /** 禁用文件的后缀标签文案（如"已禁用"） */
  disabledLabel: string
  disabled?: boolean
  size?: 'small' | 'medium'
  fullWidth?: boolean
  formControlSx?: SxProps<Theme>
}

/**
 * 插件文件选择下拉：包插件按目录分组，目录头可展开/收起。
 *
 * 实现说明：目录头是带哨兵 value 的 MenuItem，onChange 拦截哨兵只切换展开
 * 状态并保持菜单打开（受控 open + 抑制一次 onClose）；收起组的文件项用
 * display:none 隐藏而非移除，保证选中值始终能命中 children。
 */
export default function PluginFileSelect({
  files,
  value,
  onChange,
  label,
  disabledLabel,
  disabled = false,
  size = 'medium',
  fullWidth = true,
  formControlSx,
}: PluginFileSelectProps) {
  const [open, setOpen] = useState(false)
  const [expandedPackages, setExpandedPackages] = useState<Set<string>>(new Set())
  const suppressCloseRef = useRef(false)
  const groups = useMemo(() => groupPluginFiles(files), [files])

  const handleOpen = () => {
    // 打开菜单时自动展开当前选中文件所在的包
    const selectedPackage = topPackageOf(value)
    if (selectedPackage) {
      setExpandedPackages(prev => {
        if (prev.has(selectedPackage)) return prev
        const next = new Set(prev)
        next.add(selectedPackage)
        return next
      })
    }
    setOpen(true)
  }

  const handleClose = () => {
    if (suppressCloseRef.current) {
      suppressCloseRef.current = false
      return
    }
    setOpen(false)
  }

  const handleChange = (event: SelectChangeEvent<string>) => {
    const nextValue = event.target.value
    if (nextValue.startsWith(PACKAGE_TOGGLE_PREFIX)) {
      const packageName = nextValue.slice(PACKAGE_TOGGLE_PREFIX.length)
      suppressCloseRef.current = true
      setExpandedPackages(prev => {
        const next = new Set(prev)
        if (next.has(packageName)) {
          next.delete(packageName)
        } else {
          next.add(packageName)
        }
        return next
      })
      return
    }
    setOpen(false)
    onChange(event)
  }

  return (
    <FormControl fullWidth={fullWidth} size={size} sx={formControlSx} disabled={disabled}>
      <InputLabel>{label}</InputLabel>
      <Select
        value={value}
        label={label}
        open={open}
        onOpen={handleOpen}
        onClose={handleClose}
        onChange={handleChange}
        MenuProps={{ PaperProps: { sx: { maxHeight: 360 } } }}
      >
        {groups.flatMap(group => {
          const isExpanded = !group.isPackage || expandedPackages.has(group.topName)
          const nodes = []
          if (group.isPackage) {
            nodes.push(
              <MenuItem
                key={`pkg:${group.topName}`}
                value={`${PACKAGE_TOGGLE_PREFIX}${group.topName}`}
                sx={{ fontWeight: 700, color: 'text.secondary' }}
              >
                <Box component="span" sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
                  {isExpanded ? (
                    <ExpandMoreIcon fontSize="small" sx={{ opacity: 0.7 }} />
                  ) : (
                    <ChevronRightIcon fontSize="small" sx={{ opacity: 0.7 }} />
                  )}
                  <FolderOutlinedIcon fontSize="small" sx={{ opacity: 0.8 }} />
                  <Box component="span" sx={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {group.topName}/
                  </Box>
                </Box>
              </MenuItem>
            )
          }
          for (const entry of group.entries) {
            nodes.push(
              <MenuItem
                key={entry.path}
                value={entry.path}
                sx={{
                  display: isExpanded ? 'flex' : 'none',
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
                    pl: entry.inPackage ? 3.5 : 0,
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
          return nodes
        })}
      </Select>
    </FormControl>
  )
}
