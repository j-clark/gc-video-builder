import { spawn } from "node:child_process";
import path from "node:path";

type BridgeEnvelope<T> = {
  ok: boolean;
  data?: T;
  error?: string;
};

const REPO_ROOT = path.resolve(process.cwd(), "..");
const PYTHON =
  process.env.PYTHON_BIN ?? path.join(REPO_ROOT, ".venv", "bin", "python");

export async function runBridge<T>(
  action: string,
  payload: Record<string, unknown> = {},
  token?: string,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, ["gc_season_bridge.py", action], {
      cwd: REPO_ROOT,
      env: token ? { ...process.env, GC_TOKEN: token } : process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`${action} timed out.`));
    }, 15 * 60 * 1000);

    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      const output = Buffer.concat(stdout).toString("utf8").trim();
      let envelope: BridgeEnvelope<T> | undefined;
      try {
        envelope = JSON.parse(output) as BridgeEnvelope<T>;
      } catch {
        envelope = undefined;
      }
      if (code === 0 && envelope?.ok && envelope.data !== undefined) {
        resolve(envelope.data);
        return;
      }
      const detail =
        envelope?.error ||
        Buffer.concat(stderr).toString("utf8").trim() ||
        output ||
        `Bridge exited with status ${code}.`;
      reject(new Error(detail));
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

export const repoRoot = REPO_ROOT;
