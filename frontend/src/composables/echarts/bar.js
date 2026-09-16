import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart } from 'echarts/charts'
import { TooltipComponent, GridComponent } from 'echarts/components'

let installed = false

export function ensureBarChartsRegistered() {
  if (installed) return
  use([CanvasRenderer, BarChart, TooltipComponent, GridComponent])
  installed = true
}
