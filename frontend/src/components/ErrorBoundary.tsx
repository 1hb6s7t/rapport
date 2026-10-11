import { Component, type ReactNode } from "react";
import { Link } from "react-router-dom";

/** Any render-time exception becomes a readable card instead of a white page. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div role="alert" className="card max-w-xl mx-auto mt-8 space-y-3">
        <h2 className="text-base font-semibold">这个页面出错了</h2>
        <p className="text-sm text-ink-2">{this.state.error.message}</p>
        <p className="text-xs text-ink-3">数据没有丢失；这是界面渲染错误。刷新页面或返回首页即可。如需反馈，请在设置页生成诊断信息（不含任何聊天内容）。</p>
        <div className="flex gap-2">
          <button className="btn btn-primary py-1" onClick={() => this.setState({ error: null })}>重试</button>
          <Link className="btn btn-ghost py-1" to="/">回到首页</Link>
        </div>
      </div>
    );
  }
}

export function NotFoundPage() {
  return (
    <div className="card max-w-xl mx-auto mt-8 space-y-2">
      <h2 className="text-base font-semibold">没有这个页面</h2>
      <p className="text-sm text-ink-2">地址可能来自旧版本。<Link to="/">回到首页</Link></p>
    </div>
  );
}
