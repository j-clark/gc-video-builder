import { NextRequest, NextResponse } from "next/server";

import { runBridge } from "@/lib/server-bridge";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const data = await runBridge("projects");
    return NextResponse.json({ data });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 500 },
    );
  }
}

export async function POST(request: NextRequest) {
  try {
    const body = (await request.json()) as {
      action?: string;
      payload?: Record<string, unknown>;
    };
    if (!body.action) {
      return NextResponse.json({ error: "Missing bridge action." }, { status: 400 });
    }
    const data = await runBridge(
      body.action,
      body.payload ?? {},
      request.headers.get("x-gc-token") || undefined,
    );
    return NextResponse.json({ data });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 500 },
    );
  }
}
