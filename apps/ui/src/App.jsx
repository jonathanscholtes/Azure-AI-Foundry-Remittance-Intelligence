import { Routes, Route, Navigate } from "react-router-dom";
import AppLayout from "./components/layout/AppLayout.jsx";
import DashboardPage from "./pages/DashboardPage.jsx";
import ProcessPage from "./pages/ProcessPage.jsx";
import AuditPage from "./pages/AuditPage.jsx";
import RunDetailsPage from "./pages/RunDetailsPage.jsx";
import DailyPage from "./pages/DailyPage.jsx";
import WeeklyPage from "./pages/WeeklyPage.jsx";
import PlaceholderPage from "./pages/PlaceholderPage.jsx";

export default function App() {
  return (
    <AppLayout>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/home" element={<DashboardPage />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/process" element={<ProcessPage />} />
        <Route path="/rules" element={<PlaceholderPage title="Processing Rules" />} />
        <Route path="/summaries" element={<DailyPage />} />
        <Route path="/summaries/weekly" element={<WeeklyPage />} />
        <Route path="/weekly" element={<WeeklyPage />} />
        <Route path="/runs" element={<AuditPage key="runs" />} />
        <Route path="/runs/:processingId" element={<RunDetailsPage />} />
        <Route
          path="/review"
          element={
            <AuditPage
              key="review"
              title="Needs review"
              description="Remittances waiting for an analyst's decision, with the agents' recommendation and the evidence behind it."
              defaultStatus="ReviewRequired"
            />
          }
        />
        {/* Old links and bookmarks. */}
        <Route path="/exceptions" element={<Navigate to="/review" replace />} />
        <Route
          path="/audit"
          element={<AuditPage key="audit" title="Audit logs" description="Every run with its full state-transition timeline (select a row)." />}
        />
        <Route path="/reference" element={<PlaceholderPage title="Reference Data" />} />
        <Route path="/admin" element={<PlaceholderPage title="Administration" />} />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </AppLayout>
  );
}
