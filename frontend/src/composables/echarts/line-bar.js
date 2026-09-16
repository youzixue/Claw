import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart } from 'echarts/charts'
import { TooltipComponent, GridComponent, LegendComponent } from 'echarts/components'

let installed = false

export function ensureLineBarChartsRegistered() {
  if (installed) return
  use([CanvasRenderer, LineChart, BarChart, TooltipComponent, GridComponent, LegendComponent])
  installed = true
}
