export type JobStatus =
  | "queued" | "running" | "extraction_complete" | "partial_success"
  | "failed" | "interrupted";

export type PipelineStage =
  | "queued" | "collecting" | "preprocessing" | "ai_processing"
  | "awaiting_rights_approval" | "delivering" | "completed"
  | "partial_success" | "blocked" | "failed";

export interface JobSummary {
  job_id: string;
  status: JobStatus | string;
  website_url?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface JobRecord extends JobSummary {
  instagram_url?: string | null;
  facebook_url?: string | null;
  sources?: Record<string, unknown>;
  preprocessing?: { status?: string; [k: string]: unknown } | null;
  ai_curation?: { status?: string; provider?: string; model?: string; error?: string; errors?: string[]; [k: string]: unknown } | null;
  manifest_path?: string | null;
  orchestration?: unknown;
  include_undated_website?: boolean;
  error?: string | null;
}

export interface PipelineStatus {
  job_id: string;
  stage: PipelineStage | string;
  terminal: boolean;
  job_status: string;
  detail: string;
  ai_status?: string | null;
  delivery_status?: string | null;
  updated_at?: string | null;
}

export type ManifestFrame = {
  local_path?: string;
  timestamp_sec?: number;
};

export type ManifestAsset = {
  asset_id: string;
  platform?: string;
  type?: string;
  local_path?: string;
  sha256?: string;
  bytes?: number;
  mime?: string;
  width?: number | null;
  height?: number | null;
  duration_sec?: number | null;
  rights_status?: string;
  extraction_status?: string;
  source_url?: string;
  source_caption?: string | null;
  published_at_utc?: string | null;
  publication_date_source?: string | null;
  publication_date_confidence?: string | null;
  frames?: ManifestFrame[];
  duplicate_of?: string | null;
};

export interface Manifest {
  schema_version?: string;
  job_id?: string;
  website_url?: string | null;
  assets?: ManifestAsset[];
  counts?: unknown;
  coverage?: unknown;
  source_status?: string;
  collection_window?: { start_utc?: string; end_utc?: string };
}

export interface CurationImageRec {
  asset_id: string;
  recommended_section?: string;
  hero_rank?: number | null;
  overall_score?: number;
  focal_point?: string;
  crop_guidance?: string;
  strengths?: string[];
  weaknesses?: string[];
  rights_review_required?: boolean;
}

export interface CurationVideoRec {
  asset_id: string;
  recommended_section?: string;
  overall_score?: number;
  best_thumbnail_frame_index?: number | null;
  strengths?: string[];
  weaknesses?: string[];
  rights_review_required?: boolean;
}

export interface CurationRejected {
  asset_id: string;
  reason?: string;
  reasons?: string[];
}

interface CurationSelection {
  mode?: string;
  selected_asset_ids?: string[];
  rights_review_required_ids?: string[];
  best_images?: CurationImageRec[];
  best_videos?: CurationVideoRec[];
  rejected_assets?: CurationRejected[];
  selection_summary?: Record<string, unknown>;
  analysis_coverage?: { mock?: boolean; [k: string]: unknown };
}

export interface Curation extends CurationSelection {
  status?: string;
  ai_envelope?: {
    mock?: boolean;
    prompt_version?: string;
    prompt_sha256?: string;
    include_undated_website?: boolean;
    [k: string]: unknown;
  };
  fixture?: string;
  curation?: CurationSelection;
  [k: string]: unknown;
}

export interface RightsDecisionEntry {
  sequence: number;
  revision: number;
  asset_id: string;
  asset_sha: string;
  manifest_sha: string;
  actor: string;
  decision: "verified" | "rejected" | "revoked" | string;
  reason: string;
  evidence_ref: string;
  utc: string;
}

export interface RightsReview {
  job_id: string;
  revision: number;
  manifest_sha256: string;
  state: string;
  etag: string;
  verified_asset_ids: string[];
  verified_rights: Record<string, { actor: string; evidence_ref: string; reason: string; utc: string; decision: string }>;
  history: RightsDecisionEntry[];
}

export interface Delivery {
  status?: string;
  reports_status?: string;
  folder_name?: string;
  drive_folder_id?: string | null;
  drive_folder_link?: string | null;
  subfolders?: Record<string, string>;
  uploaded?: Array<{ asset_id: string; filename?: string; target_subfolder?: string; status: string; drive_file_id?: string; drive_link?: string; error?: string }>;
  reports_uploaded?: Array<{ report_id?: string; filename?: string; target_subfolder?: string; status: string; drive_file_id?: string; drive_link?: string; error?: string }>;
  pending_rights_ids?: string[];
  errors?: string[];
}

export function isMockCuration(c: Curation | null): boolean {
  if (!c) return false;
  if (c.ai_envelope?.mock === true || c.analysis_coverage?.mock === true ||
      c.curation?.analysis_coverage?.mock === true) return true;
  const markers = ["mock", "fixture", "test-only"];
  const hay = [
    String(c.ai_envelope?.prompt_version || ""),
    String(c.fixture || ""),
  ].join(" ").toLowerCase();
  return markers.some((m) => hay.includes(m));
}

export function curationSelection(c: Curation | null): CurationSelection | undefined {
  return c && !isMockCuration(c) ? c.curation ?? (c.selected_asset_ids ? c : undefined) : undefined;
}
