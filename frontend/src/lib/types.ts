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

export interface Curation {
  status?: string;
  mode?: string;
  selected_asset_ids?: string[];
  rights_review_required_ids?: string[];
  ai_envelope?: { mock?: boolean; prompt_version?: string };
  fixture?: string;
  analysis_coverage?: { mock?: boolean };
  curation?: { analysis_coverage?: { mock?: boolean } };
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
  const markers = ["mock", "fixture", "test-only"];
  const hay = [
    String(c.ai_envelope?.mock),
    String(c.analysis_coverage?.mock),
    String(c.ai_envelope?.prompt_version || ""),
    String(c.fixture || ""),
    String(c.curation?.analysis_coverage?.mock),
  ].join(" ").toLowerCase();
  return markers.some((m) => hay.includes(m));
}
