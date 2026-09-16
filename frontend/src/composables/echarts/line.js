import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { TooltipComponent, GridComponent, LegendComponent } from 'echarts/components'

let installed = false

export function ensureLineChartsRegistered() {
  if (installed) return
  use([CanvasRenderer, LineChart, TooltipComponent, GridComponent, LegendComponent])
  installed = true
}
