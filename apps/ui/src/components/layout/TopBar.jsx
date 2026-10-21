import {
  makeStyles,
  tokens,
  Text,
  Title3,
  Avatar,
} from "@fluentui/react-components";
import { brand } from "../../branding.js";

const useStyles = makeStyles({
  root: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "16px",
    flexShrink: 0,
    minHeight: "54px",
    padding: "7px 18px",
    backgroundColor: tokens.colorNeutralBackground1,
    borderBottom: `1px solid ${tokens.colorNeutralStroke2}`,
    zIndex: 10,
    "@media (max-width: 700px)": { padding: "8px 10px", gap: "8px" },
  },
  titles: { display: "flex", flexDirection: "column", gap: "0" },
  title: { fontFamily: '"Segoe UI", sans-serif', fontSize: "23px", fontWeight: 700, lineHeight: 1.05, color: "#0a2454" },
  subtitle: { color: "#526b91", fontSize: "11px", lineHeight: 1.2 },
  right: { display: "flex", alignItems: "center", gap: "12px", "@media (max-width: 700px)": { gap: "6px" } },
  user: { display: "flex", alignItems: "center", gap: "8px" },
  userText: { display: "flex", flexDirection: "column", lineHeight: 1.15, textAlign: "right" },
  userName: { fontWeight: 600, fontSize: "11px", color: "#0a2454" },
  userRole: { color: "#526b91", fontSize: "10px" },
  avatar: { backgroundColor: "#0878d1", color: "#ffffff" },
});

export default function TopBar() {
  const styles = useStyles();
  return (
    <header className={styles.root}>
      <div className={styles.titles}>
        <Title3 className={styles.title}>{brand.appTitle}</Title3>
        <Text size={200} className={styles.subtitle}>
          {brand.appSubtitle}
        </Text>
      </div>
      <div className={styles.right}>
        <div className={styles.user}>
          <div className={styles.userText}>
            <span className={styles.userName}>Jonathan Scholtes</span>
            <span className={styles.userRole}>Administrator</span>
          </div>
          <Avatar className={styles.avatar} name="Jonathan Scholtes" color="colorful" />
        </div>
      </div>
    </header>
  );
}
