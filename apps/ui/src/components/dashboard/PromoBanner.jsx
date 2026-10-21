import { makeStyles, tokens } from "@fluentui/react-components";
import { brand } from "../../branding.js";

const useStyles = makeStyles({
  root: {
    height: "100%",
    minHeight: "176px",
    borderRadius: tokens.borderRadiusLarge,
    overflow: "hidden",
    // Matches the banner artwork, so the letterbox fill around a contained image is invisible.
    backgroundColor: "#10264d",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
  },
  // object-fit: contain keeps the banner's native aspect ratio - it is never stretched or cropped,
  // just scaled to fit whatever width/height the grid cell gives it.
  image: { width: "100%", height: "100%", objectFit: "contain", display: "block" },
});

export default function PromoBanner() {
  const styles = useStyles();
  return (
    <div className={styles.root}>
      <img
        className={styles.image}
        src="/images/promo-banner.png"
        alt={`${brand.bannerTitle} ${brand.bannerSubtitle}`}
      />
    </div>
  );
}
