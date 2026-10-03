// Last-resort fallback for render errors (B18-R15): no message, no stack, just a reload.
// With a reset key (the route inside the layout), navigating elsewhere clears it (B18-RV10).
import { Component, type ReactNode } from "react";

export const RENDER_ERROR_TITLE = "Đã xảy ra lỗi hiển thị";

interface Props {
  children?: ReactNode;
  resetKey?: string;
}

interface State {
  failed: boolean;
  resetKey?: string;
}

export class AppErrorBoundary extends Component<Props, State> {
  state: State = { failed: false, resetKey: this.props.resetKey };

  static getDerivedStateFromError(_error: unknown): Partial<State> {
    return { failed: true };
  }

  static getDerivedStateFromProps(props: Props, state: State): State | null {
    if (props.resetKey === state.resetKey) return null;
    return { failed: false, resetKey: props.resetKey };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="page page-narrow" role="alert">
        <h1>{RENDER_ERROR_TITLE}</h1>
        <p>Trang gặp lỗi khi hiển thị. Dữ liệu trên máy chủ không bị ảnh hưởng.</p>
        <p>
          <button type="button" className="primary" onClick={() => window.location.reload()}>
            Tải lại trang
          </button>
        </p>
      </div>
    );
  }
}
