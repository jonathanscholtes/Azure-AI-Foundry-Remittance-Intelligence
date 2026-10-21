import { useNavigate } from "react-router-dom";
import { makeStyles, tokens, Card, Title3, Text, Badge, Link } from "@fluentui/react-components";
import { Pulse24Regular, ChevronRight16Regular } from "@fluentui/react-icons";
import { duration, number, PIPELINE_STEPS, stepIndexForState } from "../../format.js";

const useStyles = makeStyles({
  card: { padding: "10px 12px", boxSizing: "border-box", height: "100%" },
  head: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "6px" },
  heading: { display: "flex", alignItems: "center", gap: "8px" },
  title: { fontFamily: '"Segoe UI", sans-serif', fontSize: "16px", lineHeight: 1.2, fontWeight: 700, color: "#0a2454" },
  icon: { color: "#0a3c78", display: "flex" },
  link: { display: "flex", alignItems: "center", gap: "2px", fontSize: "11px" },
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(96px, 1fr))", gap: "8px" },
  metric: {
    display: "flex",
    flexDirection: "column",
    padding: "6px 8px",
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground2,
  },
  label: { fontSize: "10px", color: tokens.colorNeutralForeground3 },
  value: { fontSize: "18px", fontWeight: 700, color: "#0a2454" },
  stages: { display: "flex", gap: "6px", flexWrap: "wrap", marginTop: "8px" },
});

function Metric({ label, value, tone }) {
  const styles = useStyles();
  return (
    <div className={styles.metric}>
      <span className={styles.label}>{label}</span>
      <span className={styles.value} style={tone ? { color: tone } : undefined}>
        {value}
      </span>
    </div>
  );
}

// Live view of the Service Bus queue + Durable work in flight (polled by the parent).
export default function LivePipelineCard({ status, showLink = true }) {
  const styles = useStyles();
  const navigate = useNavigate();
  const queue = status?.queue;
  const byStep = {};
  for (const [state, count] of Object.entries(status?.inFlightByState || {})) {
    const step = PIPELINE_STEPS[stepIndexForState(state)]?.label || state;
    byStep[step] = (byStep[step] || 0) + count;
  }

  return (
    <Card className={styles.card}>
      <div className={styles.head}>
        <div className={styles.heading}>
          <Pulse24Regular className={styles.icon} />
          <Title3 className={styles.title}>Live Pipeline</Title3>
          <Badge appearance="tint" color={status ? "success" : "informative"}>{status ? "live" : "connecting"}</Badge>
        </div>
        {showLink && (
          <Link className={styles.link} as="button" onClick={() => navigate("/process")}>
            Monitor <ChevronRight16Regular />
          </Link>
        )}
      </div>
      <div className={styles.grid}>
        <Metric label="Queued (Service Bus)" value={queue && !queue.error ? number(queue.active) : "—"} />
        <Metric label="In flight" value={number(status?.inFlight ?? 0)} />
        <Metric
          label="Dead-letter"
          value={queue && !queue.error ? number(queue.deadLetter) : "—"}
          tone={queue?.deadLetter ? tokens.colorPaletteRedForeground1 : undefined}
        />
        <Metric label={`Done (${status?.windowMinutes || 60}m)`} value={number(status?.processedInWindow ?? 0)} />
        <Metric label="Per minute" value={status?.throughputPerMinute ?? "—"} />
        <Metric label="p95 end-to-end" value={duration(status?.p95DurationMs)} />
      </div>
      <div className={styles.stages}>
        {Object.keys(byStep).length === 0 ? (
          <Text size={200} italic style={{ color: tokens.colorNeutralForeground3 }}>
            Nothing in flight.
          </Text>
        ) : (
          Object.entries(byStep).map(([step, count]) => (
            <Badge key={step} appearance="outline" color="brand">
              {step}: {count}
            </Badge>
          ))
        )}
      </div>
    </Card>
  );
}
