import { useNavigate, useLocation } from "react-router-dom";
import { makeStyles, tokens } from "@fluentui/react-components";
import {
  Home24Regular,
  ArrowUpload24Regular,
  Flow24Regular,
  Play24Regular,
  DataBarVertical24Regular,
  Warning24Regular,
  DocumentBulletList24Regular,
  History24Regular,
  Database24Regular,
  People24Regular,
  Settings24Regular,
  VehicleTruck24Filled,
} from "@fluentui/react-icons";
import { brand, shell } from "../../branding.js";

export const NAV_ITEMS = [
  { path: "/home", label: "Home", icon: <Home24Regular /> },
  { path: "/dashboard", label: "File Upload", icon: <ArrowUpload24Regular /> },
  { path: "/process", label: "Live Pipeline", icon: <Flow24Regular /> },
  { path: "/runs", label: "Processing Runs", icon: <Play24Regular /> },
  { path: "/summaries", label: "Summaries", icon: <DataBarVertical24Regular /> },
  { path: "/review", label: "Needs Review", icon: <Warning24Regular /> },
  { path: "/audit", label: "Audit Logs", icon: <History24Regular /> },
  { path: "/reference", label: "Reference Data", icon: <Database24Regular /> },
  { path: "/admin", label: "Administration", icon: <People24Regular /> },
];

const useStyles = makeStyles({
  root: {
    width: "192px",
    minWidth: "192px",
    background: `linear-gradient(180deg, ${shell.navyDeep}, ${shell.navy} 62%, #123969)`,
    color: shell.sidebarText,
    display: "flex",
    flexDirection: "column",
    height: "100vh",
    position: "sticky",
    top: 0,
    "@media (max-width: 700px)": { width: "62px", minWidth: "62px" },
  },
  brand: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    padding: "0",
    minHeight: "61px",
    backgroundColor: "#f5f8fc",
    borderBottom: `1px solid ${shell.navyBorder}`,
    "@media (max-width: 700px)": { justifyContent: "center", padding: "8px 4px" },
  },
  logoMark: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    width: "32px",
    height: "32px",
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: "#e0ecfa",
    color: shell.navyActive,
    flexShrink: 0,
  },
  brandImage: { display: "block", width: "100%", height: "auto", objectFit: "contain", "@media (max-width: 700px)": { display: "none" } },
  mobileLogo: { display: "none", "@media (max-width: 700px)": { display: "flex" } },
  nav: { display: "flex", flexDirection: "column", padding: "8px 6px", gap: "2px", flex: 1 },
  item: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "7px 7px",
    borderRadius: tokens.borderRadiusMedium,
    color: shell.sidebarText,
    cursor: "pointer",
    fontSize: "11px",
    border: "none",
    background: "transparent",
    textAlign: "left",
    width: "100%",
    ":hover": { backgroundColor: shell.navyHover, color: "#ffffff" },
    "@media (max-width: 700px)": { justifyContent: "center", padding: "10px 4px", fontSize: 0 },
  },
  itemActive: {
    backgroundColor: shell.navyActive,
    color: "#ffffff",
    ":hover": { backgroundColor: shell.navyActive },
  },
  icon: { display: "flex", fontSize: "21px" },
  footerCard: {
    margin: "0",
    padding: "0",
    flex: 1,
    minHeight: "205px",
    backgroundColor: shell.navyDeep,
    backgroundImage: "url('/images/sidebar-freight.png')",
    backgroundSize: "cover",
    backgroundPosition: "center",
    "@media (max-width: 700px)": { display: "none" },
  },
});

export default function Sidebar() {
  const styles = useStyles();
  const navigate = useNavigate();
  const location = useLocation();

  return (
    <aside className={styles.root}>
      <div className={styles.brand}>
        <img className={styles.brandImage} src="/images/brand-logo.png" alt="Foundry Freight Lines" />
        <span className={`${styles.logoMark} ${styles.mobileLogo}`}>
          <VehicleTruck24Filled />
        </span>
      </div>

      <nav className={styles.nav}>
        {NAV_ITEMS.map((item) => {
          const active = item.path === "/summaries" || item.path === "/runs"
            ? location.pathname.startsWith(item.path)
            : location.pathname === item.path;
          return (
            <button
              key={item.path}
              className={`${styles.item} ${active ? styles.itemActive : ""}`}
              title={item.label}
              onClick={() => navigate(item.path)}
            >
              <span className={styles.icon}>{item.icon}</span>
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>

      <div className={styles.footerCard} role="img" aria-label={brand.tagline} />
    </aside>
  );
}
