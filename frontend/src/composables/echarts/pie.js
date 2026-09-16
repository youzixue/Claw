import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { PieChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent } from 'echarts/components'

let installed = false

export function ensurePieChartsRegistered() {
  if (installed) return
  use([CanvasRenderer, PieChart, TooltipComponent, LegendComponent])
  installed = true
}
