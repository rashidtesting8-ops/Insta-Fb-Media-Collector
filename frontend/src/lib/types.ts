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
  preprocessing?: unknown;
  ai_curation?: unknown;
  manifest_path?: string | null;
  orchestration?: unknown;
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

export type ManifestAsset = {
  asset_id: string;
  local_path?: string;
  sha256?: string;
  bytes?: number;
  mime?: string;
  rights_status?: string;
  extraction_status?: string;
  source_url?: string;
};

export interface Manifest {
  schema_version?: string;
  job_id?: string;
  assets?: ManifestAsset[];
  counts?: unknown;
  coverage?: unknown;
  source_status?: string;
}

interface CurationSelection {
  mode?: string;
  selected_asset_ids?: string[];
  rights_review_required_ids?: string[];
  analysis_coverage?: { mock?: boolean };
}

export interface Curation extends CurationSelection {
  status?: string;
  ai_envelope?: { mock?: boolean; prompt_version?: string };
  fixture?: string;
  curation?: CurationSelection;
  [k: string]: unknown;
}

export interface Delivery {
  status?: string;
  folder_name?: string;
  drive_folder_id?: string | null;
  drive_folder_link?: string | null;
  uploaded?: Array<{ asset_id: string; status: string; drive_file_id?: string; drive_link?: string; error?: string }>;
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
