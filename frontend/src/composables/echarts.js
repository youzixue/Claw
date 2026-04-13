import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart, PieChart, GaugeChart, RadarChart, ScatterChart, EffectScatterChart, SankeyChart, CandlestickChart } from 'echarts/charts'
import {
  TitleComponent, TooltipComponent, LegendComponent, GridComponent,
  VisualMapComponent, DataZoomComponent, ToolboxComponent,
} from 'echarts/components'

let installed = false

export function ensureEChartsRegistered() {
  if (installed) return
  use([
    CanvasRenderer,
    LineChart, BarChart, PieChart, GaugeChart, RadarChart, ScatterChart, EffectScatterChart, SankeyChart, CandlestickChart,
    TitleComponent, TooltipComponent, LegendComponent, GridComponent,
    VisualMapComponent, DataZoomComponent, ToolboxComponent,
  ])
  installed = true
}
