import { useCallback, useMemo, useState } from "react";
import { makeStyles } from "@fluentui/react-components";
import { api } from "../api.js";
import { isTerminal } from "../format.js";
import { usePolling } from "../usePolling.js";
import UploadCard from "../components/dashboard/UploadCard.jsx";
import UploadGuidelines from "../components/dashboard/UploadGuidelines.jsx";
import PromoBanner from "../components/dashboard/PromoBanner.jsx";
import RecentFilesTable from "../components/dashboard/RecentFilesTable.jsx";
import ProcessingWorkflowCard from "../components/dashboard/ProcessingWorkflowCard.jsx";
import ProcessingSummary from "../components/dashboard/ProcessingSummary.jsx";
import ExceptionsTable from "../components/dashboard/ExceptionsTable.jsx";
import LivePipelineCard from "../components/dashboard/LivePipelineCard.jsx";

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", gap: "12px" },
  rowTop: {
    display: "grid",
    gridTemplateColumns: "1.45fr 1fr 0.9fr",
    gridTemplateRows: "minmax(176px, auto)",
    gap: "12px",
    alignItems: "stretch",
    "@media (max-width: 1100px)": { gridTemplateColumns: "1.2fr 1fr", gridTemplateRows: "repeat(2, minmax(176px, auto))" },
    "@media (max-width: 700px)": { gridTemplateColumns: "1fr", gridTemplateRows: "repeat(3, minmax(164px, auto))" },
  },
  rowFlow: {
    display: "grid",
    gridTemplateColumns: "1.5fr 1fr",
    gap: "12px",
    alignItems: "stretch",
    "@media (max-width: 1000px)": { gridTemplateColumns: "1fr" },
  },
  rowBottom: {
    display: "grid",
    gridTemplateColumns: "0.9fr 1.1fr",
    gap: "12px",
    alignItems: "stretch",
    "@media (max-width: 800px)": { gridTemplateColumns: "1fr" },
  },
});

// Derive the dashboard KPI summary from the ledger list.
function summarize(items) {
  const isException = (r) => r.requiresReview || r.status === "ReviewRequired" || r.status === "Failed";
  return {
    count: items.length,
    completed: items.filter((r) => r.status === "Completed").length,
    reviewRequired: items.filter((r) => r.requiresReview || r.status === "ReviewRequired").length,
    failed: items.filter((r) => r.status === "Failed").length,
    exceptions: items.filter(isException),
  };
}

export default function DashboardPage() {
  const styles = useStyles();
  const [error, setError] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [tracked, setTracked] = useState(null); // { processingId, fileName }
  const [run, setRun] = useState(null);
  const [events, setEvents] = useState([]);
  const [items, setItems] = useState([]);
  const [loadingList, setLoadingList] = useState(true);
  const [pipeline, setPipeline] = useState(null);

  // Runs still moving through the pipeline, newest first (the ledger list is already sorted desc).
  const inFlight = useMemo(() => items.filter((r) => !isTerminal(r.status)), [items]);
  const anyInFlight = inFlight.length > 0 || (pipeline?.inFlight || 0) > 0 || (pipeline?.queue?.active || 0) > 0;

  const loadList = useCallback(async () => {
    try {
      const { items: rows } = await api.list({ limit: 200 });
      setItems(rows || []);
    } finally {
      setLoadingList(false);
    }
  }, []);

  // Ledger list + pipeline: fast while work is in flight, relaxed when idle.
  usePolling(loadList, anyInFlight ? 3000 : 15000);
  usePolling(async () => setPipeline(await api.pipeline(60)), anyInFlight ? 3000 : 15000);

  // Which run gets the detailed stepper: the explicitly tracked one while it is still in flight
  // (or not yet in the ledger = just queued); otherwise auto-follow the oldest run still running
  // so the card stays live; otherwise keep showing the last tracked run.
  const focusId = useMemo(() => {
    if (tracked?.processingId) {
      const trackedRun = items.find((r) => r.processingId === tracked.processingId);
      if (!trackedRun || !isTerminal(trackedRun.status)) return tracked.processingId;
    }
    if (inFlight.length) return inFlight[inFlight.length - 1].processingId;
    return tracked?.processingId || null;
  }, [tracked, items, inFlight]);

  // Poll the focus run's ledger row + live event timeline until it reaches a terminal state.
  const focusTerminal = run?.processingId === focusId && isTerminal(run?.status);
  usePolling(
    async () => {
      const [detail, timeline] = await Promise.all([api.get(focusId), api.events(focusId)]);
      setRun(detail);
      setEvents((timeline.events || []).filter((e) => !detail.runId || e.runId === detail.runId));
    },
    2000,
    Boolean(focusId) && !focusTerminal,
    [focusId]
  );

  const handleFiles = useCallback(async (files) => {
    const accepted = files.filter((f) => /\.(msg|eml)$/i.test(f.name));
    if (!accepted.length) {
      setError("Please choose Outlook .msg (or .eml) files.");
      return;
    }
    setError(accepted.length < files.length ? "Some files were skipped (only .msg / .eml are accepted)." : null);
    let last = null;
    for (let i = 0; i < accepted.length; i += 1) {
      const file = accepted[i];
      setUploading(accepted.length > 1 ? `Queuing ${i + 1} of ${accepted.length}...` : true);
      try {
        const queued = await api.upload(file);
        last = { processingId: queued.processingId, fileName: file.name };
      } catch (e) {
        setError(`${file.name}: ${e.message || "Upload failed."}`);
      }
    }
    setUploading(false);
    if (last) {
      setTracked(last);
      setRun({ processingId: last.processingId, status: "Queued", currentState: "Queued", sourceName: last.fileName });
      setEvents([]);
      loadList();
    }
  }, [loadList]);

  const summary = summarize(items);

  return (
    <div className={styles.page}>
      <div className={styles.rowTop}>
        <UploadCard onFiles={handleFiles} fileName={tracked?.fileName} uploading={uploading} error={error} />
        <UploadGuidelines />
        <PromoBanner />
      </div>

      <div className={styles.rowFlow}>
        <ProcessingWorkflowCard
          run={run}
          events={events}
          fileName={tracked?.fileName}
          inFlight={inFlight}
          onFocus={(r) => setTracked({ processingId: r.processingId, fileName: r.sourceName })}
        />
        <LivePipelineCard status={pipeline} />
      </div>

      <RecentFilesTable items={items} loading={loadingList} />

      <div className={styles.rowBottom}>
        <ProcessingSummary summary={summary} />
        <ExceptionsTable items={summary.exceptions} />
      </div>
    </div>
  );
}
