import { makeStyles, tokens, Card, Text } from "@fluentui/react-components";

const useStyles = makeStyles({
  // Fixed column counts (not auto-fit) so rows are always full instead of leaving an orphan card or two.
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(6, 1fr)",
    gap: "12px",
    "@media (max-width: 1200px)": { gridTemplateColumns: "repeat(4, 1fr)" },
    "@media (max-width: 800px)": { gridTemplateColumns: "repeat(3, 1fr)" },
    "@media (max-width: 520px)": { gridTemplateColumns: "repeat(2, 1fr)" },
  },
  card: { padding: "12px 14px", display: "flex", flexDirection: "column", gap: "2px" },
  label: { color: tokens.colorNeutralForeground3 },
  value: { fontSize: "22px", fontWeight: 700, lineHeight: 1.1, whiteSpace: "nowrap" },
});

export default function KpiCards({ items }) {
  const styles = useStyles();
  return (
    <div className={styles.grid}>
      {items.map((item) => (
        <Card key={item.label} className={styles.card}>
          <Text size={200} className={styles.label}>
            {item.label}
          </Text>
          <Text className={styles.value} style={item.color ? { color: item.color } : undefined}>
            {item.value}
          </Text>
        </Card>
      ))}
    </div>
  );
}
