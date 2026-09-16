import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { CandlestickChart, LineChart, BarChart, RadarChart } from 'echarts/charts'
import { TooltipComponent, GridComponent, DataZoomComponent, LegendComponent, RadarComponent, GraphicComponent } from 'echarts/components'

let installed = false

export function ensureStockDetailChartsRegistered() {
  if (installed) return
  use([
    CanvasRenderer,
    CandlestickChart,
    LineChart,
    BarChart,
    RadarChart,
    TooltipComponent,
    GridComponent,
    DataZoomComponent,
    LegendComponent,
    RadarComponent,
    GraphicComponent,
  ])
  installed = true
}
