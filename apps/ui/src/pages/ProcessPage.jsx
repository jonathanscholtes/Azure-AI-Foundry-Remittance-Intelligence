import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  makeStyles,
  tokens,
  Card,
  CardHeader,
  Title3,
  Text,
  Button,
  Badge,
  MessageBar,
  MessageBarBody,
  Table,
  TableHeader,
  TableRow,
  TableHeaderCell,
  TableBody,
  TableCell,
} from "@fluentui/react-components";
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid, Legend } from "recharts";
import { api } from "../api.js";
import { usePolling } from "../usePolling.js";
import { dateTime, duration, statusLabel } from "../format.js";
import LivePipelineCard from "../components/dashboard/LivePipelineCard.jsx";
import WorkflowStepper from "../components/WorkflowStepper.jsx";

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", gap: "12px" },
  row: { display: "grid", gridTemplateColumns: "1fr 1.3fr", gap: "12px", "@media (max-width: 1000px)": { gridTemplateColumns: "1fr" } },
  clickable: { cursor: "pointer" },
  muted: { color: tokens.colorNeutralForeground3 },
});

// Live pipeline monitor: Service Bus depth, Durable work in flight, throughput, and each active run.
export default function ProcessPage() {
  const styles = useStyles();
  const navigate = useNavigate();
  const [status, setStatus] = useState(null);
  const [inFlight, setInFlight] = useState([]);
  const [notice, setNotice] = useState(null);

  const load = useCallback(async () => {
    const [pipeline, list] = await Promise.all([api.pipeline(60), api.list({ status: "InFlight", limit: 100 })]);
    setStatus(pipeline);
    setInFlight(list.items || []);
  }, []);
  usePolling(load, 2500);

  const replay = async () => {
    try {
      const { replayed } = await api.replayDeadLetters();
      setNotice(`Re-queued ${replayed} dead-lettered message(s).`);
    } catch (e) {
      setNotice(`Replay failed: ${e.message}`);
    }
  };

  const buckets = (status?.buckets || []).map((b) => ({
    ...b,
    time: new Date(b.start).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
  }));

  return (
    <div className={styles.page}>
      <div className={styles.row}>
        <LivePipelineCard status={status} showLink={false} />
        <Card>
          <CardHeader header={<Title3>Throughput (last {status?.windowMinutes || 60} min)</Title3>} />
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={buckets}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="time" fontSize={10} />
              <YAxis allowDecimals={false} fontSize={10} />
              <Tooltip />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Bar dataKey="completed" stackId="a" name="Processed" fill="#107C10" />
              <Bar dataKey="reviewRequired" stackId="a" name="Needs review" fill="#F7A600" />
              <Bar dataKey="failed" stackId="a" name="Failed" fill="#C50F1F" />
              <Bar dataKey="duplicate" stackId="a" name="Duplicate" fill="#8A8886" />
            </BarChart>
          </ResponsiveContainer>
          <Text size={200} className={styles.muted}>
            p50 {duration(status?.p50DurationMs)} · p95 {duration(status?.p95DurationMs)} · avg queue wait {duration(status?.avgQueueLatencyMs)} ·
            Content Understanding throttle retries {status?.throttleRetries ?? 0}
          </Text>
        </Card>
      </div>

      {(status?.queue?.deadLetter || 0) > 0 && (
        <MessageBar intent="error">
          <MessageBarBody>
            {status.queue.deadLetter} message(s) are in the Service Bus dead-letter queue (malformed or repeatedly failing intake).{" "}
            <Button size="small" onClick={replay}>
              Replay dead letters
            </Button>
          </MessageBarBody>
        </MessageBar>
      )}
      {status?.queue?.error && (
        <MessageBar intent="warning">
          <MessageBarBody>Queue metrics unavailable ({status.queue.error}).</MessageBarBody>
        </MessageBar>
      )}
      {notice && (
        <MessageBar intent="info">
          <MessageBarBody>{notice}</MessageBarBody>
        </MessageBar>
      )}

      <Card style={{ overflowX: "auto" }}>
        <CardHeader
          header={<Title3>In flight</Title3>}
          description={<Text>Runs currently moving through the Durable orchestration (refreshes every few seconds).</Text>}
          action={<Badge appearance="tint">{inFlight.length}</Badge>}
        />
        {/* Auto layout: Fluent's fixed layout ignored the Progress min-width, so the stepper overran Submitted. */}
        <Table size="small" aria-label="In-flight runs" style={{ tableLayout: "auto", fontSize: "12px" }}>
          <TableHeader>
            <TableRow>
              <TableHeaderCell>File</TableHeaderCell>
              <TableHeaderCell>Stream</TableHeaderCell>
              <TableHeaderCell>State</TableHeaderCell>
              <TableHeaderCell style={{ minWidth: 560, width: "50%" }}>Progress</TableHeaderCell>
              <TableHeaderCell style={{ whiteSpace: "nowrap" }}>Submitted</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {inFlight.map((r) => (
              <TableRow key={`${r.processingId}-${r.runId}`} className={styles.clickable} onClick={() => navigate(`/runs/${r.processingId}`)}>
                <TableCell style={{ overflowWrap: "anywhere" }}>{r.sourceName || r.processingId}</TableCell>
                <TableCell>{r.stream || "—"}</TableCell>
                <TableCell style={{ whiteSpace: "nowrap" }}>{statusLabel(r.currentState || r.status)}</TableCell>
                <TableCell>
                  <WorkflowStepper run={r} compact hidePendingSub />
                </TableCell>
                <TableCell style={{ whiteSpace: "nowrap" }}>{dateTime(r.submittedAtUtc || r.createdAtUtc)}</TableCell>
              </TableRow>
            ))}
            {inFlight.length === 0 && (
              <TableRow>
                <TableCell colSpan={5}>
                  <Text italic className={styles.muted}>
                    Nothing in flight. Upload files on the File Upload page or run scripts/load_test.py.
                  </Text>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </Card>
    </div>
  );
}
