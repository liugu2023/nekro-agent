import { Plugin } from '../../services/api/plugins'

/** 从插件文件路径提取顶层模块名（包目录名或去掉 .py/.py.disabled 后缀的文件名） */
export function topModuleNameOf(filePath: string): string {
  return filePath.split('/')[0].replace(/\.py(\.disabled)?$/, '')
}

/**
 * 在已加载插件列表中查找文件对应的插件。
 *
 * 按声明模块名与插件 key（author.moduleName）尾段宽容匹配；文件未被加载为
 * 插件（如 .py.disabled 文件、加载失败、目录缺少 __init__.py）时返回 null。
 */
export function findPluginByFile(plugins: Plugin[], filePath: string): Plugin | null {
  const topModule = topModuleNameOf(filePath)
  if (!topModule) return null
  return (
    plugins.find(
      plugin =>
        !plugin.loadFailed &&
        (plugin.moduleName === topModule ||
          plugin.id === topModule ||
          plugin.id.endsWith(`.${topModule}`))
    ) ?? null
  )
}
