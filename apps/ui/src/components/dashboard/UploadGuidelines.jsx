import { makeStyles, tokens, Card, Text, Title3 } from "@fluentui/react-components";
import { CheckmarkCircle16Filled, Info20Filled } from "@fluentui/react-icons";

const GUIDELINES = [
  "Upload the remittance as an Outlook .msg or MIME .eml email file.",
  "Each email should contain a single remittance advice.",
  "The remittance date is read from the document automatically.",
  "Duplicate files are detected and skipped automatically.",
];

const useStyles = makeStyles({
  card: { padding: "12px 14px 16px", height: "100%", minHeight: 0, boxSizing: "border-box" },
  heading: { display: "flex", alignItems: "center", gap: "8px", color: "#0a2454" },
  title: { fontFamily: '"Segoe UI", sans-serif', fontSize: "16px", lineHeight: 1.2, fontWeight: 700, color: "#0a2454" },
  info: { color: "#086bd8", display: "flex" },
  list: { display: "flex", flexDirection: "column", gap: "6px", marginTop: "8px" },
  row: { display: "flex", gap: "10px", alignItems: "flex-start" },
  icon: { color: tokens.colorPaletteGreenForeground1, flexShrink: 0, marginTop: "2px" },
  text: { color: tokens.colorNeutralForeground2, fontSize: "11px", lineHeight: 1.25 },
});

export default function UploadGuidelines() {
  const styles = useStyles();
  return (
    <Card className={styles.card}>
      <div className={styles.heading}>
        <Info20Filled className={styles.info} />
        <Title3 className={styles.title}>File Upload Guidelines</Title3>
      </div>
      <div className={styles.list}>
        {GUIDELINES.map((g) => (
          <div className={styles.row} key={g}>
            <CheckmarkCircle16Filled className={styles.icon} />
            <Text className={styles.text}>{g}</Text>
          </div>
        ))}
      </div>
    </Card>
  );
}
