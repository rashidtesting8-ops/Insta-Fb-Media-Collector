import { approvalRoute } from "@/lib/approval-route";

export function GET(req: Request, ctx: { params: Promise<{ id: string }> }) {
  return approvalRoute(req, ctx, "panel");
}
