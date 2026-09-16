import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { CandlestickChart, LineChart, BarChart } from 'echarts/charts'
import { TooltipComponent, GridComponent, DataZoomComponent, LegendComponent } from 'echarts/components'

let installed = false

export function ensureKlineChartsRegistered() {
  if (installed) return
  use([
    CanvasRenderer,
    CandlestickChart,
    LineChart,
    BarChart,
    TooltipComponent,
    GridComponent,
    DataZoomComponent,
    LegendComponent,
  ])
  installed = true
}
