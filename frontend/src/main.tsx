import { lazy, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createBrowserRouter } from "react-router-dom";
import "./index.css";
import "./styles/watercolor.css";
import { ErrorBoundary, NotFoundPage } from "@/components/ErrorBoundary";
import { Layout } from "@/components/Layout";
import { WelcomePage } from "@/pages/Welcome";
import { connectLocalSession } from "@/api/client";
import { Button, ErrorBox, Spinner } from "@/components/ui";
const ImportPage = lazy(() => import("@/pages/Import").then(module => ({ default: module.ImportPage })));
const ReviewPage = lazy(() => import("@/pages/Review").then(module => ({ default: module.ReviewPage })));
const ProfilePage = lazy(() => import("@/pages/Profile").then(module => ({ default: module.ProfilePage })));
const PreviewPage = lazy(() => import("@/pages/Preview").then(module => ({ default: module.PreviewPage })));
const ProgressPage = lazy(() => import("@/pages/Progress").then(module => ({ default: module.ProgressPage })));
const ReportPage = lazy(() => import("@/pages/Report").then(module => ({ default: module.ReportPage })));
const SettingsPage = lazy(() => import("@/pages/Settings").then(module => ({ default: module.SettingsPage })));
const DiaryPage = lazy(() => import("@/pages/Diary").then(module => ({ default: module.DiaryPage })));
const RepliesPage = lazy(() => import("@/pages/Replies").then(module => ({ default: module.RepliesPage })));
const HistoryPage = lazy(() => import("@/pages/History").then(module => ({ default: module.HistoryPage })));
const InboxPage = lazy(() => import("@/pages/Inbox").then(module => ({ default: module.InboxPage })));
const WechatPage = lazy(() => import("@/pages/Wechat").then(module => ({ default: module.WechatPage })));
const WorkbenchPage = lazy(() => import("@/pages/Workbench").then(module => ({ default: module.WorkbenchPage })));

const qc = new QueryClient({ defaultOptions: { queries: { retry: 0, refetchOnWindowFocus: false } } });

const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    errorElement: <ErrorBoundary><NotFoundPage /></ErrorBoundary>,
    children: [
      { index: true, element: <WelcomePage /> },
      { path: "import", element: <ImportPage /> },
      { path: "wechat", element: <WechatPage /> },
      { path: "workbench", element: <WorkbenchPage /> },
      { path: "review", element: <ReviewPage /> },
      { path: "profile", element: <ProfilePage /> },
      { path: "preview/:analysisId", element: <PreviewPage /> },
      { path: "progress/:analysisId", element: <ProgressPage /> },
      { path: "report", element: <ReportPage /> },
      { path: "report/:reportId", element: <ReportPage /> },
      { path: "diary", element: <DiaryPage /> },
      { path: "replies", element: <RepliesPage /> },
      { path: "history", element: <HistoryPage /> },
      { path: "inbox", element: <InboxPage /> },
      { path: "settings", element: <SettingsPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);

const root = createRoot(document.getElementById("root")!);

async function openWorkspace() {
  root.render(<div className="p-8"><Spinner label="正在连接本机服务…" /></div>);
  try {
    await connectLocalSession();
    root.render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <ErrorBoundary>
        <RouterProvider router={router} />
      </ErrorBoundary>
    </QueryClientProvider>
  </StrictMode>,
    );
  } catch (error) {
    root.render(<div className="mx-auto max-w-xl space-y-4 p-8"><h1>连接本机服务</h1><ErrorBox error={error} /><Button onClick={() => void openWorkspace()}>重新连接</Button></div>);
  }
}

void openWorkspace();
