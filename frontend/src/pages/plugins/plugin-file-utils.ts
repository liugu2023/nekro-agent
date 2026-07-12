export interface PluginToggleTarget {
  /** 插件当前是否处于禁用状态 */
  isDisabled: boolean
  /** 启停时需要重命名的现存文件（单文件插件为自身，包插件为其 __init__.py 入口） */
  sourcePath: string
  /** 重命名后的目标路径 */
  targetPath: string
}

/**
 * 计算插件启停（启用/禁用）操作的目标文件。
 *
 * 单文件插件切换自身的 .disabled 后缀；包插件切换包入口 __init__.py 的
 * .disabled 后缀（加载器只加载含 __init__.py 的目录，重命名入口即整包禁用）。
 * 包目录缺少入口文件时返回 null，表示无法启停。
 */
export function getPluginToggleTarget(files: string[], selectedFile: string): PluginToggleTarget | null {
  if (!selectedFile) return null
  const separatorIndex = selectedFile.indexOf('/')
  if (separatorIndex <= 0) {
    const isDisabled = selectedFile.endsWith('.disabled')
    return {
      isDisabled,
      sourcePath: selectedFile,
      targetPath: isDisabled ? selectedFile.replace(/\.disabled$/, '') : `${selectedFile}.disabled`,
    }
  }
  const initPath = `${selectedFile.slice(0, separatorIndex)}/__init__.py`
  const disabledInitPath = `${initPath}.disabled`
  if (files.includes(initPath)) {
    return { isDisabled: false, sourcePath: initPath, targetPath: disabledInitPath }
  }
  if (files.includes(disabledInitPath)) {
    return { isDisabled: true, sourcePath: disabledInitPath, targetPath: initPath }
  }
  return null
}
