import { Plugin } from '../../services/api/plugins'

/** 从插件文件路径提取顶层模块名（包目录名或去掉 .py/.py.disabled 后缀的文件名） */
export function topModuleNameOf(filePath: string): string {
  return filePath.split('/')[0].replace(/\.py(\.disabled)?$/, '')
}

/** 是否为可通过文件重命名重新启用的插件入口。 */
export function isDisabledPluginEntry(filePath: string): boolean {
  if (!filePath.endsWith('.py.disabled')) return false
  const parts = filePath.split('/')
  return parts.length === 1 || parts[parts.length - 1] === '__init__.py.disabled'
}

/**
 * 解析文件所属插件的入口文件路径。
 *
 * 插件的启停单位是入口文件（顶层单文件插件本身，或包插件的 __init__.py）。
 * 选中包内普通模块时，启停应作用于包入口而不是被选中的那个文件。
 * 找不到入口（文件列表未加载完成等）时回退为原路径。
 */
export function resolvePluginEntryFile(filePath: string, allFiles: string[]): string {
  const parts = filePath.split('/')
  if (parts.length <= 1) return filePath
  const topModule = parts[0]
  return (
    allFiles.find(item => item === `${topModule}/__init__.py`) ??
    allFiles.find(item => item === `${topModule}/__init__.py.disabled`) ??
    filePath
  )
}

/**
 * 在已加载插件列表中查找文件对应的插件。
 *
 * 按声明模块名与插件 key（author.moduleName）尾段宽容匹配；文件未被加载为
 * 插件（如 .py.disabled 文件、目录缺少 __init__.py）时返回 null。
 * 加载失败的插件仍需返回，以便编辑器为其提供文件级禁用入口。
 */
export function findPluginByFile(plugins: Plugin[], filePath: string): Plugin | null {
  const topModule = topModuleNameOf(filePath)
  if (!topModule) return null
  return (
    plugins.find(
      plugin =>
        plugin.moduleName === topModule ||
          plugin.id === topModule ||
          plugin.id.endsWith(`.${topModule}`)
    ) ?? null
  )
}
