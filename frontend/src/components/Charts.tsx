import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { LineChart, BarChart, ScatterChart } from "echarts/charts";
import { GridComponent, TooltipComponent, MarkLineComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { TrendPoint } from "@/api/types";
import { CONF_LABELS, DIM_LABELS, DIM_ORDER, MOMENTUM_LABELS } from "@/i18n/labels";

echarts.use([LineChart, BarChart, ScatterChart, GridComponent, TooltipComponent, MarkLineComponent, CanvasRenderer]);

const ACCENT = "#aa658d"; // 与水彩主题的玫瑰灰紫主线一致
// one hue per dimension, matching `.dim-*` in index.css
const DIM_COLORS: Record<string, string> = { responsiveness: "#8c7aaa", initiative: "#aa658d", investment: "#4d947f", warmth: "#c87998", future: "#c4946b" };
const INK3 = "#806f83";
const LINE = "#ead7e4";

function useChart(build: (c: echarts.ECharts) => void, deps: unknown[]) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const c = echarts.init(ref.current, undefined, { renderer: "canvas" });
    build(c);
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(ref.current);
    return () => {
      ro.disconnect();
      c.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}

export function TrendChart({ points, momentum, onPick }: { points: TrendPoint[]; momentum: string; onPick?: (p: TrendPoint) => void }) {
  const ref = useChart(
    (c) => {
      c.setOption({
        animation: false,
        grid: { left: 36, right: 16, top: 16, bottom: 28 },
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "line", lineStyle: { color: INK3, width: 1, type: "dashed" } },
          backgroundColor: "#fff",
          borderColor: LINE,
          textStyle: { color: "#29232d", fontSize: 12 },
          formatter: (params: unknown) => {
            const p = (params as { dataIndex: number }[])[0];
            const pt = points[p.dataIndex];
            const score = pt.insufficient ? "��本不足，不计分" : `好感度指数 ${pt.score}`;
            return `<div><b>${pt.label}</b><br/>${score}<br/><span style="color:${INK3}">消息 ${pt.n_msgs} 条，对方文本 ${pt.n_other_text} 条</span></div>`;
          },
        },
        xAxis: { type: "category", data: points.map((p) => p.label), axisLine: { lineStyle: { color: LINE } }, axisTick: { show: false }, axisLabel: { color: INK3, fontSize: 11 } },
        yAxis: { type: "value", min: 0, max: 100, interval: 25, splitLine: { lineStyle: { color: LINE } }, axisLabel: { color: INK3, fontSize: 11 } },
        series: [
          {
            type: "line",
            data: points.map((p) => (p.insufficient ? null : p.score)),
            connectNulls: false,
            smooth: 0.4,
            lineStyle: { color: ACCENT, width: 2.4 },
            areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: "rgba(170,101,141,0.16)" }, { offset: 1, color: "rgba(170,101,141,0.01)" }] } },
            itemStyle: { color: ACCENT, borderColor: "#fff", borderWidth: 2 },
            symbol: "circle",
            symbolSize: (_: unknown, params: { dataIndex: number }) => Math.max(8, Math.min(16, 6 + points[params.dataIndex].n_msgs / 4)),
            markLine: { silent: true, symbol: "none", lineStyle: { color: LINE, type: "dashed" }, label: { show: true, formatter: "50 · 中性参考线（不是及格线）", color: INK3, fontSize: 10, position: "insideEndTop" }, data: [{ yAxis: 50 }] },
          },
          {
            type: "scatter",
            data: points.map((p) => (p.insufficient ? 50 : null)),
            symbol: "emptyCircle",
            symbolSize: 8,
            itemStyle: { color: INK3, opacity: 0.6 },
            tooltip: { show: false },
          },
        ],
      });
      // Clicking a point opens its drill-down. The chart instance is rebuilt whenever `deps`
      // change, so this never stacks handlers.
      if (onPick) {
        c.on("click", (params: unknown) => {
          const i = (params as { dataIndex?: number }).dataIndex;
          if (typeof i === "number" && points[i]) onPick(points[i]);
        });
      }
    },
    [points, onPick],
  );
  return (
    <div>
      <div ref={ref} className="h-52 w-full" role="img" aria-label={`好感度趋势，动量${MOMENTUM_LABELS[momentum]}`} />
      <details className="text-xs text-ink-3 mt-1">
        <summary className="cursor-pointer">表格视图</summary>
        <table className="mt-1 w-full">
          <thead><tr className="text-left"><th>分段</th><th>指数</th><th>消息数</th></tr></thead>
          <tbody>{points.map((p) => <tr key={p.label}><td>{p.label}</td><td>{p.insufficient ? "样本不足" : p.score}</td><td>{p.n_msgs}</td></tr>)}</tbody>
        </table>
      </details>
    </div>
  );
}

export function DimBars({ dims, onPick }: { dims: Record<string, { score: number | null; confidence: string; evidence_ids: string[] }>; onPick?: (dim: string) => void }) {
  const ref = useChart(
    (c) => {
      const labels = DIM_ORDER.map((k) => DIM_LABELS[k]);
      const vals = DIM_ORDER.map((k) => dims[k]?.score ?? null);
      c.setOption({
        animation: false,
        grid: { left: 64, right: 40, top: 6, bottom: 6 },
        tooltip: {
          trigger: "item",
          backgroundColor: "#fff",
          borderColor: LINE,
          textStyle: { color: "#29232d", fontSize: 12 },
          formatter: (p: unknown) => {
            const { dataIndex } = p as { dataIndex: number };
            const k = DIM_ORDER[dataIndex];
            const d = dims[k];
            return `<b>${DIM_LABELS[k]}</b><br/>${d?.score ?? "不可用"}${d ? `<br/><span style="color:${INK3}">置信度 ${CONF_LABELS[d.confidence] ?? d.confidence} · 证据 ${d.evidence_ids.length} 条</span>` : ""}`;
          },
        },
        xAxis: { type: "value", min: 0, max: 100, show: false },
        yAxis: { type: "category", data: labels, inverse: true, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: "#4a444f", fontSize: 12 } },
        series: [
          {
            type: "bar",
            data: vals.map((v, i) => ({ value: v ?? 0, itemStyle: v === null ? { color: "transparent" } : { color: DIM_COLORS[DIM_ORDER[i]] ?? ACCENT, borderRadius: [6, 6, 6, 6] } })),
            barWidth: 12,
            showBackground: true,
            backgroundStyle: { color: "#f9e9ee", borderRadius: [6, 6, 6, 6] },
            label: { show: true, position: "right", color: "#1a1523", fontSize: 12, formatter: (p: { dataIndex: number }) => (vals[p.dataIndex] === null ? "—" : String(vals[p.dataIndex])) },
          },
        ],
      });
      c.on("click", (e: { dataIndex: number }) => onPick?.(DIM_ORDER[e.dataIndex]));
    },
    [dims],
  );
  return <div ref={ref} className="h-44 w-full" role="img" aria-label="五维分项" />;
}
