import { useNavigate } from "react-router-dom";
import { makeStyles, tokens, Card, Title3, Text, Link, Badge, Spinner } from "@fluentui/react-components";
import { Settings24Regular, ChevronRight16Regular } from "@fluentui/react-icons";
import WorkflowStepper from "../WorkflowStepper.jsx";
import { PIPELINE_STEPS, statusLabel, stepIndexForState } from "../../format.js";

const stageLabel = (r) =>
  PIPELINE_STEPS[Math.max(0, stepIndexForState(r.currentState || r.status))]?.label ||
  statusLabel(r.currentState || r.status);

const useStyles = makeStyles({
  card: { padding: "10px 12px", height: "100%", minHeight: "132px", boxSizing: "border-box", display: "flex", flexDirection: "column" },
  head: { display: "flex", alignItems: "center", justifyContent: "space-between" },
  heading: { display: "flex", alignItems: "center", gap: "8px", color: "#0a2454" },
  title: { fontFamily: '"Segoe UI", sans-serif', fontSize: "16px", lineHeight: 1.2, fontWeight: 700, color: "#0a2454" },
  headingIcon: { color: "#0a3c78", display: "flex" },
  subtitle: { color: tokens.colorNeutralForeground3, fontSize: "10px", lineHeight: 1.2 },
  link: { display: "flex", alignItems: "center", gap: "2px", fontSize: "11px" },
  others: {
    display: "flex",
    flexDirection: "column",
    gap: "1px",
    marginTop: "auto",
    paddingTop: "6px",
    borderTop: `1px solid ${tokens.colorNeutralStroke2}`,
  },
  othersLabel: { color: tokens.colorNeutralForeground3, fontSize: "10px", fontWeight: 600, marginBottom: "2px" },
  row: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "3px 4px",
    borderRadius: tokens.borderRadiusSmall,
    cursor: "pointer",
    ":hover": { backgroundColor: tokens.colorNeutralBackground2 },
  },
  rowName: { flex: 1, fontSize: "11px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  rowStage: { fontSize: "10px", color: tokens.colorNeutralForeground3, whiteSpace: "nowrap" },
});

export default function ProcessingWorkflowCard({ run, events, fileName, inFlight = [], onFocus }) {
  const styles = useStyles();
  const navigate = useNavigate();
  // Other runs still processing, excluding the one shown in the detailed stepper.
  const others = inFlight.filter((r) => r.processingId !== run?.processingId);

  return (
    <Card className={styles.card}>
      <div className={styles.head}>
        <div className={styles.heading}>
          <Settings24Regular className={styles.headingIcon} />
          <Title3 className={styles.title}>Processing Workflow</Title3>
          {inFlight.length > 1 && (
            <Badge appearance="tint" color="brand">
              {inFlight.length} in flight
            </Badge>
          )}
        </div>
        {run?.processingId && (
          <Link className={styles.link} as="button" onClick={() => navigate(`/runs/${run.processingId}`)}>
            Run details <ChevronRight16Regular />
          </Link>
        )}
      </div>
      <Text className={styles.subtitle}>
        {run
          ? `${fileName || run.sourceName || run.processingId} · ${statusLabel(run.currentState || run.status)} · ${run.processingId}`
          : "Upload a remittance to track it live: Service Bus → Durable orchestration → Content Understanding → validation → agent assurance."}
      </Text>
      <WorkflowStepper run={run} events={events} compact />

      {others.length > 0 && (
        <div className={styles.others}>
          <Text className={styles.othersLabel}>Also in flight ({others.length})</Text>
          {others.slice(0, 4).map((r) => (
            <div
              key={r.processingId}
              className={styles.row}
              onClick={() => onFocus?.(r)}
              title={`Track ${r.sourceName || r.processingId}`}
            >
              <Spinner size="extra-tiny" />
              <Text className={styles.rowName}>{r.sourceName || r.processingId}</Text>
              <Text className={styles.rowStage}>{stageLabel(r)}</Text>
            </div>
          ))}
          {others.length > 4 && (
            <Link className={styles.link} as="button" onClick={() => navigate("/process")}>
              + {others.length - 4} more <ChevronRight16Regular />
            </Link>
          )}
        </div>
      )}
    </Card>
  );
}
