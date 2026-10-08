/**
 * 触发浏览器保存一个 Blob。
 *
 * 两个细节都是实测踩出来的，不是「以防万一」：
 * 1. `<a>` 必须先挂到文档里再 click()：游离节点在部分浏览器里不会触发下载。
 * 2. 不能在 click() 之后立刻 revokeObjectURL：下载是异步读取 blob 的，立刻撤销会让稍大的文件
 *    静默失败——实测 38KB 的 pptx 页面上照样显示「已保存 财务分析报告-2026-10.pptx（38 KB）」，
 *    磁盘上却一个文件都没有。延迟释放，读取交给浏览器。
 */
export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
}
