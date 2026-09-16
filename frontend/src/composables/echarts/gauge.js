import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { GaugeChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'

let installed = false

export function ensureGaugeChartsRegistered() {
  if (installed) return
  use([CanvasRenderer, GaugeChart, TooltipComponent])
  installed = true
}
