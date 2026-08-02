import fs from "node:fs";
import path from "node:path";
import { Readable } from "node:stream";

import { NextRequest, NextResponse } from "next/server";

import { repoRoot } from "@/lib/server-bridge";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function safeMediaPath(project: string, relativePath: string): string {
  const projectsRoot = path.resolve(repoRoot, "gc_seasons");
  const projectRoot = path.resolve(projectsRoot, project);
  const mediaPath = path.resolve(projectRoot, relativePath);
  if (
    !project ||
    projectRoot === projectsRoot ||
    !projectRoot.startsWith(`${projectsRoot}${path.sep}`) ||
    !mediaPath.startsWith(`${projectRoot}${path.sep}`)
  ) {
    throw new Error("Invalid media path.");
  }
  return mediaPath;
}

export async function GET(request: NextRequest) {
  try {
    const project = request.nextUrl.searchParams.get("project") ?? "";
    const relativePath = request.nextUrl.searchParams.get("path") ?? "";
    const mediaPath = safeMediaPath(project, relativePath);
    const stat = await fs.promises.stat(mediaPath);
    const range = request.headers.get("range");
    const headers = new Headers({
      "Accept-Ranges": "bytes",
      "Content-Type": "video/mp4",
      "Cache-Control": "private, max-age=3600",
    });

    if (!range) {
      headers.set("Content-Length", String(stat.size));
      return new NextResponse(
        Readable.toWeb(fs.createReadStream(mediaPath)) as ReadableStream,
        { headers },
      );
    }

    const match = /bytes=(\d+)-(\d*)/.exec(range);
    if (!match) {
      return new NextResponse(null, { status: 416 });
    }
    const start = Number(match[1]);
    const end = match[2] ? Math.min(Number(match[2]), stat.size - 1) : stat.size - 1;
    if (start >= stat.size || end < start) {
      return new NextResponse(null, { status: 416 });
    }
    headers.set("Content-Length", String(end - start + 1));
    headers.set("Content-Range", `bytes ${start}-${end}/${stat.size}`);
    return new NextResponse(
      Readable.toWeb(fs.createReadStream(mediaPath, { start, end })) as ReadableStream,
      { status: 206, headers },
    );
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 404 },
    );
  }
}
