/**
 * 通用导出工具 — 支持Excel(xlsx)和CSV
 */
import * as XLSX from 'xlsx'
import { notifyWarning } from '@/utils/message'

/**
 * 导出数据为Excel文件
 * @param {Array<Object>} data - 数据数组
 * @param {Array<{key: string, label: string, format?: Function}>} columns - 列定义
 * @param {string} filename - 文件名(不含扩展名)
 * @param {string} sheetName - 工作表名
 */
export function exportToExcel(data, columns, filename = 'export', sheetName = 'Sheet1') {
  if (!data || data.length === 0) {
    notifyWarning('暂无数据可导出')
    return
  }

  // 构建表头和行数据
  const headers = columns.map(c => c.label)
  const rows = data.map(item =>
    columns.map(col => {
      const val = item[col.key]
      return col.format ? col.format(val, item) : (val ?? '--')
    })
  )

  const wsData = [headers, ...rows]
  const ws = XLSX.utils.aoa_to_sheet(wsData)

  // 设置列宽
  ws['!cols'] = columns.map(c => ({ wch: Math.max(c.label.length * 2, 12) }))

  const wb = XLSX.utils.book_new()
  XLSX.utils.book_append_sheet(wb, ws, sheetName)
  XLSX.writeFile(wb, `${filename}.xlsx`)
}

function buildSheet(data = [], columns = []) {
  const headers = columns.map(c => c.label)
  const rows = (data || []).map(item =>
    columns.map(col => {
      const val = item[col.key]
      return col.format ? col.format(val, item) : (val ?? '--')
    })
  )
  const wsData = [headers, ...rows]
  const ws = XLSX.utils.aoa_to_sheet(wsData)
  ws['!cols'] = columns.map(c => ({ wch: c.width || Math.max(c.label.length * 2, 12) }))
  if (columns.length > 0) {
    ws['!autofilter'] = {
      ref: XLSX.utils.encode_range({
        s: { r: 0, c: 0 },
        e: { r: Math.max(rows.length, 1), c: columns.length - 1 },
      }),
    }
  }
  return ws
}

export function exportWorkbookToExcel(sheets = [], filename = 'export') {
  if (!Array.isArray(sheets) || sheets.length === 0) {
    notifyWarning('暂无数据可导出')
    return
  }

  const validSheets = sheets.filter(sheet => Array.isArray(sheet?.columns) && sheet.columns.length)
  if (validSheets.length === 0) {
    notifyWarning('暂无数据可导出')
    return
  }

  const hasData = validSheets.some(sheet => Array.isArray(sheet?.data) && sheet.data.length > 0)
  if (!hasData) {
    notifyWarning('暂无数据可导出')
    return
  }

  const wb = XLSX.utils.book_new()
  validSheets.forEach((sheet, index) => {
    const name = sheet.sheetName || sheet.name || `Sheet${index + 1}`
    const ws = buildSheet(sheet.data || [], sheet.columns || [])
    XLSX.utils.book_append_sheet(wb, ws, name.slice(0, 31))
  })
  XLSX.writeFile(wb, `${filename}.xlsx`)
}

/**
 * 导出数据为CSV文件
 * @param {Array<Object>} data - 数据数组
 * @param {Array<{key: string, label: string, format?: Function}>} columns - 列定义
 * @param {string} filename - 文件名(不含扩展名)
 */
export function exportToCSV(data, columns, filename = 'export') {
  if (!data || data.length === 0) {
    notifyWarning('暂无数据可导出')
    return
  }

  const headers = columns.map(c => c.label)
  const rows = data.map(item =>
    columns.map(col => {
      const val = item[col.key]
      const formatted = col.format ? col.format(val, item) : (val ?? '--')
      // CSV中含逗号/换行需要加引号
      const str = String(formatted)
      return str.includes(',') || str.includes('\n') ? `"${str}"` : str
    }).join(',')
  )

  const csvContent = '\uFEFF' + [headers.join(','), ...rows].join('\n')
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${filename}.csv`
  a.click()
  URL.revokeObjectURL(url)
}

/**
 * 格式化辅助函数
 */
export const formatters = {
  percent: (v) => v != null ? `${Number(v).toFixed(2)}%` : '--',
  billion: (v) => v != null ? `${(Number(v) / 1e8).toFixed(2)}亿` : '--',
  yiUnit: (v) => v != null ? `${Number(v).toFixed(1)}亿` : '--',  // 已是亿单位
  yesNo: (v) => v ? '是' : '否',
  round2: (v) => v != null ? Number(v).toFixed(2) : '--',
}
