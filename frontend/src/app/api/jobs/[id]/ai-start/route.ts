import { approvalRoute } from "@/lib/approval-route";

export function POST(req: Request, ctx: { params: Promise<{ id: string }> }) {
  return approvalRoute(req, ctx, "start");
}
