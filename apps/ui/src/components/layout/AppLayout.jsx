import { makeStyles, tokens, Text } from "@fluentui/react-components";
import Sidebar from "./Sidebar.jsx";
import TopBar from "./TopBar.jsx";
import { brand } from "../../branding.js";

const useStyles = makeStyles({
  // Fixed-height viewport: only the content region scrolls, so the TopBar and footer stay pinned.
  root: { display: "flex", height: "100vh", overflow: "hidden", backgroundColor: "#edf2f8" },
  main: { flex: 1, display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0 },
  content: {
    flex: 1,
    minHeight: 0,
    overflowY: "auto",
    padding: "12px 16px",
    "@media (max-width: 700px)": { padding: "10px" },
  },
  footer: {
    flexShrink: 0,
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    padding: "8px 16px",
    borderTop: `1px solid ${tokens.colorNeutralStroke2}`,
    backgroundColor: tokens.colorNeutralBackground1,
    color: tokens.colorNeutralForeground3,
    "@media (max-width: 600px)": { display: "none" },
  },
  foundry: { display: "flex", alignItems: "center", gap: "6px" },
  foundryLogo: { height: "16px", width: "auto", display: "block" },
});

export default function AppLayout({ children }) {
  const styles = useStyles();
  return (
    <div className={styles.root}>
      <Sidebar />
      <div className={styles.main}>
        <TopBar />
        <main className={styles.content}>{children}</main>
        <footer className={styles.footer}>
          <Text size={200}>Demonstration application</Text>
          <div className={styles.foundry}>
            <img
              className={styles.foundryLogo}
              src="/images/foundry_logo.png"
              alt="Microsoft Foundry"
              onError={(e) => {
                e.currentTarget.style.display = "none"; // hide gracefully until the asset is added
              }}
            />
            <Text size={200}>Powered by Microsoft Foundry</Text>
          </div>
        </footer>
      </div>
    </div>
  );
}
