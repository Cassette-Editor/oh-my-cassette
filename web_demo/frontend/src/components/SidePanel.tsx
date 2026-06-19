import { useApp } from "../useCassette";
import type { Translate } from "../i18n";
import type { Asset, Job } from "../types";

interface Props {
  open: boolean;
  onClose: () => void;
}

export function SidePanel({ open, onClose }: Props) {
  const { t, assets, jobs, send } = useApp();
  return (
    <aside className={`side-pane ${open ? "open" : ""}`} aria-label={t("statusAria")}>
      <button type="button" className="drawer-close" onClick={onClose} aria-label={t("statusClose")}>
        ✕
      </button>

      <section>
        <div className="section-title">
          <h2>{t("assetsTitle")}</h2>
          <button type="button" onClick={() => void send("/check_assets")}>
            {t("check")}
          </button>
        </div>
        <div className="asset-list">
          {assets && assets.length === 0 && <EmptyCard text={t("emptyAssets")} />}
          {assets?.map((asset, index) => (
            <AssetCard key={asset.asset_id || index} asset={asset} t={t} />
          ))}
        </div>
      </section>

      <section>
        <div className="section-title">
          <h2>{t("jobsTitle")}</h2>
          <button type="button" title={t("pauseTitle")} onClick={() => void send("/cut")}>
            {t("pause")}
          </button>
        </div>
        <div className="job-list">
          {jobs && jobs.length === 0 && <EmptyCard text={t("emptyJobs")} />}
          {jobs?.map((job, index) => (
            <JobCard key={job.job_id || index} job={job} t={t} />
          ))}
        </div>
      </section>
    </aside>
  );
}

function EmptyCard({ text }: { text: string }) {
  return <div className="status-card empty">{text}</div>;
}

type BadgeVariant = "success" | "danger" | "info" | "warn" | "neutral";

function Badge({ text, variant }: { text: string; variant: BadgeVariant }) {
  return <span className={`badge ${variant}`}>{text}</span>;
}

function mediaTypeLabel(type: string | undefined, t: Translate): string {
  if (type === "video") return t("video");
  if (type === "image") return t("image");
  if (type === "audio") return t("audio");
  return t("file");
}

function formatSize(bytes: number | undefined): string {
  if (!bytes) return "";
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function statusVariant(status: string | undefined): BadgeVariant {
  const value = String(status || "").toLowerCase();
  if (/(done|complete|success|finish|ready)/.test(value)) return "success";
  if (/(fail|error|cancel)/.test(value)) return "danger";
  if (/(run|process|pend|queue|active|start)/.test(value)) return "info";
  return "neutral";
}

function AssetCard({ asset, t }: { asset: Asset; t: Translate }) {
  const size = formatSize(asset.size_bytes);
  const missing = asset.exists === false;
  return (
    <div className="status-card">
      <span className="card-title">{asset.original_name || asset.asset_id || "asset"}</span>
      <div className="card-meta">
        <span>{mediaTypeLabel(asset.media_type, t)}</span>
        {size && (
          <>
            <span className="dot">·</span>
            <span className="mono">{size}</span>
          </>
        )}
        <span className="dot">·</span>
        <Badge text={missing ? t("assetMissing") : t("assetSaved")} variant={missing ? "danger" : "success"} />
      </div>
    </div>
  );
}

function JobCard({ job, t }: { job: Job; t: Translate }) {
  const report = job.report || {};
  const summary = report.user_summary || report.latest_progress || "";
  return (
    <div className="status-card">
      <span className="card-title">
        <span className="mono">{job.job_id || t("unknownJob")}</span>
      </span>
      <div className="card-meta">
        <Badge text={job.status || "unknown"} variant={statusVariant(job.status)} />
      </div>
      {summary && <p>{summary}</p>}
      {(job.downloads || []).map((item, index) => (
        <a key={index} href={item.url} target="_blank" rel="noreferrer">
          {t("download")} {item.filename}
        </a>
      ))}
      {job.log_url && (
        <a href={job.log_url} target="_blank" rel="noreferrer">
          {`${t("log")} ${job.job_id || ""}`.trim()}
        </a>
      )}
    </div>
  );
}
