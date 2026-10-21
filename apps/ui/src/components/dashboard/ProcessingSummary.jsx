import { useNavigate } from "react-router-dom";
import { makeStyles, tokens, Card, Title3, Text, Link } from "@fluentui/react-components";
import { ChevronRight16Regular, DataBarVertical24Regular } from "@fluentui/react-icons";
import { number } from "../../format.js";

const COLORS = {
  blue: "#2170b8",
  green: tokens.colorPaletteGreenForeground1,
  orange: "#C77700",
  red: tokens.colorPaletteRedForeground1,
};

const useStyles = makeStyles({
  card: { padding: "12px 14px", height: "100%", boxSizing: "border-box", display: "flex", flexDirection: "column" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between" },
  heading: { display: "flex", alignItems: "center", gap: "8px", color: "#0a2454" },
  title: { fontFamily: '"Segoe UI", sans-serif', fontSize: "16px", lineHeight: 1.2, fontWeight: 700, color: "#0a2454" },
  headingIcon: { color: "#0a3c78", display: "flex" },
  link: { display: "flex", alignItems: "center", gap: "2px", fontSize: "11px" },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(4, 1fr)",
    gap: "0",
    marginTop: "8px",
    flex: 1, // fill the card's leftover height so the stats center vertically instead of hugging the top
  },
  tile: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center", // center the number + label horizontally within the cell
    justifyContent: "center", // and vertically within the full-height cell
    gap: "5px",
    padding: "8px 6px",
    borderRight: `1px solid ${tokens.colorNeutralStroke2}`,
    textAlign: "center",
    ":last-child": { borderRight: "none" }, // no divider past the final tile
  },
  value: { fontSize: "28px", fontWeight: 700, lineHeight: 1 },
  label: { color: tokens.colorNeutralForeground3, fontSize: "12px" },
});

export default function ProcessingSummary({ summary }) {
  const styles = useStyles();
  const navigate = useNavigate();
  const s = summary || {};
  const tiles = [
    { label: "Total Files", value: s.count, color: COLORS.blue },
    { label: "Successfully Processed", value: s.completed, color: COLORS.green },
    { label: "Needs Review", value: s.reviewRequired, color: COLORS.orange },
    { label: "Failed", value: s.failed, color: COLORS.red },
  ];

  return (
    <Card className={styles.card}>
      <div className={styles.header}>
        <div className={styles.heading}>
          <DataBarVertical24Regular className={styles.headingIcon} />
          <Title3 className={styles.title}>Processing Summary</Title3>
        </div>
        <Link className={styles.link} onClick={() => navigate("/summaries")} as="button">
          View Details <ChevronRight16Regular />
        </Link>
      </div>
      <div className={styles.grid}>
        {tiles.map((t) => (
          <div className={styles.tile} key={t.label}>
            <Text className={styles.value} style={{ color: t.color }}>
              {number(t.value ?? 0)}
            </Text>
            <Text className={styles.label}>{t.label}</Text>
          </div>
        ))}
      </div>
    </Card>
  );
}
